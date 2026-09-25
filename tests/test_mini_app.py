from pathlib import Path
from types import SimpleNamespace

import pytest

from legal_entity_agent import telegram_bot
from legal_entity_agent.fns_client import FnsError

ROOT = Path(__file__).parents[1]


def test_mini_app_monitoring_controls_update_server_state() -> None:
    html = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    bot_source = (ROOT / "src" / "legal_entity_agent" / "telegram_bot.py").read_text(encoding="utf-8")

    assert "send('unwatch', row.query)" in html
    assert "action === 'unwatch'" in html
    assert "localStorage" in html
    assert "initDataUnsafe?.user?.id" in html
    assert "initDataUnsafe?.chat?.id" in html
    assert "params.get('chat_id')" in html
    assert "params.get('context') || fragmentParams.get('context')" in html
    assert "request_id" in html
    assert "const storageKey" in html
    assert "const persistRows" in html
    assert "const rememberQuery" in html
    assert "const valueRequired = new Set" in html
    assert "valueRequired.has(action) && !value" in html
    assert "new TextEncoder().encode(serializedPayload).length" in html
    assert "Список слишком велик для резервного режима Telegram" in html
    assert 'id="shareList"' in html
    assert "action === 'share_list'" in html
    assert "shared_list_id" in html
    assert "params.get('shared_queries')" not in html
    assert "Добавлено компаний из общего списка" in html
    assert 'id="exportExcel"' in html
    assert "send('export_excel')" in html
    assert "формате .xlsx" in html
    assert 'action == "export_excel"' in bot_source
    assert 'action not in {"check", "watch", "unwatch"}' in bot_source
    assert 'class="hero-chip"' in html
    assert "prefers-reduced-motion" in html
    forbidden_ui_token = "cap" + "tcha"
    assert forbidden_ui_token not in html.casefold()
    assert (forbidden_ui_token + "_done") not in bot_source.casefold()


def test_mini_app_link_carries_chat_context() -> None:
    bot_source = (ROOT / "src" / "legal_entity_agent" / "telegram_bot.py").read_text(encoding="utf-8")

    assert "chat_id: int | str | None = None" in bot_source
    assert "_mini_app_keyboard(chat_id=message.chat.id, chat_type=message.chat.type)" in bot_source
    assert "chat_id={quote(str(chat_id), safe='')}" in bot_source


@pytest.mark.asyncio
async def test_private_chat_uses_api_web_app_button_when_api_is_configured(monkeypatch) -> None:
    monkeypatch.setenv("MINI_APP_URL", "https://example.test/mini_app/")
    monkeypatch.setenv("MINI_APP_API_URL", "https://api.example.test/mini-app-api")

    answers: list[dict] = []

    class Message:
        chat = SimpleNamespace(id=12345, type="private")

        async def answer(self, text, **kwargs):
            answers.append({"text": text, **kwargs})

    await telegram_bot._open_check_mini_app(Message())

    assert len(answers) == 1
    keyboard = answers[0]["reply_markup"]
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].text == "Открыть Mini App"
    url = keyboard.inline_keyboard[0][0].web_app.url
    assert url.startswith(
        "https://example.test/mini_app/?chat_id=12345&api_url=https%3A%2F%2Fapi.example.test%2Fmini-app-api&transport=api&context="
    )
    assert len(url.rsplit("&context=", 1)[1]) >= 32


def test_mini_app_api_url_normalizes_trailing_slash(monkeypatch) -> None:
    monkeypatch.setenv("MINI_APP_API_URL", "https://api.example.test/mini-app-api/")

    assert telegram_bot._mini_app_api_url() == "https://api.example.test/mini-app-api"


def test_mini_app_link_keeps_existing_fragment_after_query_parameters(monkeypatch) -> None:
    monkeypatch.setenv("MINI_APP_URL", "https://example.test/mini_app/#telegram")
    monkeypatch.delenv("MINI_APP_API_URL", raising=False)

    keyboard = telegram_bot._mini_app_reply_keyboard(chat_id=12345)

    assert keyboard is not None
    assert keyboard.keyboard[0][0].web_app.url == (
        "https://example.test/mini_app/?chat_id=12345&transport=web_app_data#telegram"
    )


def test_mini_app_payload_rejects_body_too_large_for_api_limits() -> None:
    queries = [f"{'x' * 490}{index:03d}" for index in range(100)]

    with pytest.raises(ValueError, match="слишком велик"):
        telegram_bot._validate_mini_app_payload({"action": "export_excel", "queries": queries})


@pytest.mark.asyncio
async def test_check_command_runs_direct_query_without_opening_mini_app(monkeypatch) -> None:
    called: list[str] = []

    async def fake_run_check(message, query: str) -> None:
        called.append(query)

    async def fail_open(message) -> None:
        raise AssertionError("Mini App must not open for a direct /check query")

    monkeypatch.setattr(telegram_bot, "_run_check", fake_run_check)
    monkeypatch.setattr(telegram_bot, "_open_check_mini_app", fail_open)

    await telegram_bot.check(object(), SimpleNamespace(args="ИНН 7707083893"))

    assert called == ["ИНН 7707083893"]


def test_private_chat_keeps_legacy_reply_web_app_fallback(monkeypatch) -> None:
    monkeypatch.setenv("MINI_APP_URL", "https://example.test/mini_app/")
    monkeypatch.delenv("MINI_APP_API_URL", raising=False)

    keyboard = telegram_bot._mini_app_reply_keyboard(chat_id=12345)

    assert keyboard is not None
    assert keyboard.one_time_keyboard is True
    assert keyboard.keyboard[0][0].text == "Открыть Mini App"
    assert keyboard.keyboard[0][0].web_app.url == "https://example.test/mini_app/?chat_id=12345&transport=web_app_data"


@pytest.mark.asyncio
async def test_mini_app_check_reports_fns_error_to_chat(monkeypatch) -> None:
    class Access:
        def role_for(self, chat_id, *, user_id, username):
            return "checker"

    class Agent:
        async def check(self, query):
            raise FnsError("ФНС вернула неподдерживаемый ответ.")

    answers: list[str] = []

    class Message:
        chat = SimpleNamespace(id=-100123, type="group")
        from_user = SimpleNamespace(id=42, username="checker")

        async def answer(self, text, **kwargs):
            answers.append(text)

    monkeypatch.setattr(telegram_bot, "access_store", Access())
    monkeypatch.setattr(telegram_bot, "agent", Agent())
    monkeypatch.setattr(telegram_bot, "learning_store", None)
    monkeypatch.delenv("MINI_APP_URL", raising=False)

    await telegram_bot._run_check(Message(), "7707083893")

    assert answers == ["Проверка ФНС не выполнена: ФНС вернула неподдерживаемый ответ."]
