import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from legal_entity_agent import telegram_bot
from legal_entity_agent.mini_app_security import MiniAppContextStore, MiniAppRateLimiter


def _signed_init_data(bot_token: str, *, user_id: int = 42) -> str:
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("user", json.dumps({"id": user_id, "username": "checker"}, separators=(",", ":"))),
        ("query_id", "AAEAAAE"),
    ]
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode([*pairs, ("hash", digest)])


@pytest.mark.asyncio
async def test_api_uses_server_context_and_rejects_chat_id_tampering(monkeypatch) -> None:
    bot_token = "12345:test-token"
    context_store = MiniAppContextStore()
    context_token = context_store.issue(-100123, chat_type="supergroup")
    captured: list[tuple[object, dict]] = []

    async def fake_mini_app_data(message) -> None:
        captured.append((message, json.loads(message.web_app_data.data)))

    monkeypatch.setattr(telegram_bot, "mini_app_data", fake_mini_app_data)
    app = web.Application()
    app[telegram_bot._MINI_APP_BOT_KEY] = object()
    app[telegram_bot._MINI_APP_BOT_TOKEN_KEY] = bot_token
    app[telegram_bot._MINI_APP_CONTEXT_STORE_KEY] = context_store
    app[telegram_bot._MINI_APP_RATE_LIMITER_KEY] = MiniAppRateLimiter(limit=30, window_seconds=60)
    app[telegram_bot._MINI_APP_EXPENSIVE_RATE_LIMITER_KEY] = MiniAppRateLimiter(limit=8, window_seconds=60)
    app[telegram_bot._MINI_APP_SEMAPHORE_KEY] = asyncio.Semaphore(2)
    app.router.add_post(telegram_bot.MINI_APP_API_PATH, telegram_bot._mini_app_api)

    headers = {"X-Telegram-Init-Data": _signed_init_data(bot_token)}
    async with TestServer(app) as server:
        async with TestClient(server) as client:
            tampered = await client.post(
                telegram_bot.MINI_APP_API_PATH,
                headers=headers,
                json={
                    "action": "check",
                    "query": "7707083893",
                    "chat_id": -100999,
                    "context": context_token,
                    "request_id": "request-tampered-123",
                },
            )
            assert tampered.status == 401
            assert captured == []

            accepted = await client.post(
                telegram_bot.MINI_APP_API_PATH,
                headers=headers,
                json={
                    "action": "check",
                    "query": "7707083893",
                    "chat_id": -100123,
                    "context": context_token,
                    "request_id": "request-valid-123",
                },
            )
            assert accepted.status == 200
            assert (await accepted.json())["ok"] is True

            replay = await client.post(
                telegram_bot.MINI_APP_API_PATH,
                headers=headers,
                json={
                    "action": "check",
                    "query": "7707083893",
                    "chat_id": -100123,
                    "context": context_token,
                    "request_id": "request-valid-123",
                },
            )
            assert replay.status == 409

    assert len(captured) == 1
    message, payload = captured[0]
    assert message.chat.id == -100123
    assert message.chat.type == "supergroup"
    assert payload == {"action": "check", "query": "7707083893"}
