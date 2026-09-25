from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qsl, quote, urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiohttp import web
from dotenv import load_dotenv

from .agent import LegalEntityAgent
from .audit import AuditStore
from .batch_jobs import BatchJob, BatchJobRegistry, BatchJobState
from .bulk_import import parse_upload
from .chat_skill import ChatSkill, ChatSkillError
from .conversation import NaturalIntent, parse_natural_request
from .deep_check import AtomnoFnsCheckAdapter
from .excel_export import ExcelCheckRow, build_check_workbook, row_from_assessment, row_from_error
from .fns_client import FnsEgrulClient, FnsError, FnsFallbackClient, FnsNotFoundError, FnsTransientError
from .history import HistoryEntry, HistoryStore
from .identifiers import InvalidIdentifier, parse_search_query
from .learning import FeedbackLabel, LearningStore
from .licenses import LicenseStore, new_license
from .local_registry import LocalEgrulClient
from .mini_app_security import MiniAppContextStore, MiniAppRateLimiter, MiniAppSecurityError
from .models import SearchQuery
from .notifications import EmailSubscriptionStore, SmtpConfig, normalize_email, send_email
from .permissions import ChatAccessStore, configured_main_admin_user_id, is_main_admin, normalize_tag
from .reactions import ReactionSettings
from .render import format_assessment
from .shared_lists import SharedListStore
from .watchlist import WatchStore

router = Router()
agent: LegalEntityAgent | None = None
access_store: ChatAccessStore | None = None
learning_store: LearningStore | None = None
history_store: HistoryStore | None = None
license_store: LicenseStore | None = None
audit_store: AuditStore | None = None
watch_store: WatchStore | None = None
shared_list_store: SharedListStore | None = None
notification_store: EmailSubscriptionStore | None = None
smtp_config = SmtpConfig(host="")
chat_skill: ChatSkill | None = None
reaction_settings = ReactionSettings()
batch_jobs = BatchJobRegistry()
batch_tasks: dict[str, asyncio.Task[None]] = {}
MAX_SHARED_MINI_APP_URL_LENGTH = 3500
MINI_APP_API_PATH = "/mini-app-api"
MINI_APP_CONTEXT_TTL_SECONDS = 900
MINI_APP_ACTIONS = frozenset(
    {
        "check",
        "watch",
        "unwatch",
        "export_excel",
        "share_list",
        "import_shared_list",
        "set_email",
        "remove_email",
        "batch_cancel",
        "batch_status",
    }
)
MINI_APP_EXPENSIVE_ACTIONS = frozenset({"check", "export_excel", "share_list", "batch_cancel", "batch_status"})
MINI_APP_MAX_QUERY_LENGTH = 500
MINI_APP_MAX_QUERIES_PAYLOAD_BYTES = 24 * 1024
MINI_APP_SEND_DATA_MAX_BYTES = 4096
MINI_APP_MAX_EMAIL_LENGTH = 254
MINI_APP_MAX_JOB_ID_LENGTH = 128
MINI_APP_MAX_SHARED_LIST_ID_LENGTH = 128
_MINI_APP_BOT_KEY = web.AppKey("mini_app_bot", Bot)
_MINI_APP_BOT_TOKEN_KEY = web.AppKey("mini_app_bot_token", str)
_MINI_APP_CONTEXT_STORE_KEY = web.AppKey("mini_app_context_store", MiniAppContextStore)
_MINI_APP_RATE_LIMITER_KEY = web.AppKey("mini_app_rate_limiter", MiniAppRateLimiter)
_MINI_APP_EXPENSIVE_RATE_LIMITER_KEY = web.AppKey("mini_app_expensive_rate_limiter", MiniAppRateLimiter)
_MINI_APP_SEMAPHORE_KEY = web.AppKey("mini_app_semaphore", asyncio.Semaphore)

mini_app_context_store = MiniAppContextStore()

BOT_MENU_ACTIONS = frozenset(
    {
        "🔎 Проверить компанию",
        "📋 История проверок",
        "📊 Проверить все",
        "📁 Загрузить CSV/XLSX",
        "👁 Мониторинг",
        "📄 Лицензии",
        "🆘 Помощь",
        "🧠 Навыки",
    }
)


def _audit(message: Message, action: str, details: str = "") -> None:
    if audit_store and message.from_user:
        try:
            audit_store.record(chat_id=message.chat.id, actor_id=message.from_user.id, action=action, details=details)
        except Exception:
            logging.exception("Не удалось записать действие в аудит")


def _mini_app_url() -> str:
    """Возвращает безопасный адрес Mini App, опубликованный по HTTPS."""
    url = os.getenv("MINI_APP_URL", "").strip()
    return url if url.startswith("https://") else ""


def _mini_app_api_url() -> str:
    """Возвращает HTTPS-адрес API Mini App, если он настроен."""

    url = os.getenv("MINI_APP_API_URL", "").strip()
    if not url.startswith("https://"):
        return ""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        return ""
    path = parsed.path.rstrip("/")
    if path != MINI_APP_API_PATH:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _mini_app_link(
    *,
    chat_id: int | str | None = None,
    chat_type: str | None = None,
    shared_list_id: str | None = None,
    transport: str = "api",
) -> str:
    """Возвращает ссылку Mini App с непрозрачным контекстом чата.

    Для API-сценария ``chat_id`` может оставаться в URL только для UI и
    совместимости, но не является источником авторизации. Сервер выдаёт
    короткоживущий launch-контекст, который хранится только внутри процесса
    бота; каждый запрос дополнительно получает одноразовый ``request_id``.
    """

    url = _mini_app_url()
    if not url:
        return url
    params: list[str] = []
    context_token = ""
    if chat_id is not None and transport == "api" and _mini_app_api_url():
        context_token = mini_app_context_store.issue(
            chat_id,
            chat_type=chat_type,
            ttl_seconds=_positive_env_int("MINI_APP_CONTEXT_TTL_SECONDS", MINI_APP_CONTEXT_TTL_SECONDS),
        )
    if chat_id is not None:
        params.append(f"chat_id={quote(str(chat_id), safe='')}")
    if shared_list_id:
        params.append(f"shared_list_id={quote(shared_list_id, safe='')}")
    api_url = _mini_app_api_url()
    if api_url:
        params.append(f"api_url={quote(api_url, safe='')}")
    params.append(f"transport={quote(transport, safe='')}")
    if context_token:
        params.append(f"context={quote(context_token, safe='')}")
    base_url, hash_marker, fragment = url.partition("#")
    if not params:
        return url
    separator = "&" if "?" in base_url else "?"
    result = f"{base_url}{separator}{'&'.join(params)}"
    if hash_marker:
        result = f"{result}#{fragment}"
    return result


def _mini_app_keyboard(
    *,
    chat_id: int | str | None = None,
    chat_type: str | None = None,
    shared_list_id: str | None = None,
) -> InlineKeyboardMarkup | None:
    url = _mini_app_link(
        chat_id=chat_id,
        chat_type=chat_type,
        shared_list_id=shared_list_id,
        transport="api",
    )
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть Mini App", web_app=WebAppInfo(url=url))]]
    )


def _mini_app_reply_keyboard(
    *,
    chat_id: int | str | None = None,
    chat_type: str | None = None,
) -> ReplyKeyboardMarkup | None:
    """Возвращает клавиатуру запуска Mini App для личного чата.

    Telegram передаёт ``Telegram.WebApp.sendData`` боту только для Mini App,
    открытого через ``KeyboardButton(web_app=...)``. Inline-кнопка подходит
    для открытия приложения в группах, но не создаёт ``web_app_data``-сообщение.
    """

    url = _mini_app_link(chat_id=chat_id, chat_type=chat_type, transport="web_app_data")
    if not url:
        return None
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть Mini App", web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Нажмите «Открыть Mini App»",
    )


class _MiniAppMessage:
    """Минимальный контекст сообщения для действий, пришедших по HTTPS API."""

    def __init__(self, bot: Bot, chat_id: int, chat_type: str, user, payload: dict) -> None:
        self.bot = bot
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.from_user = user
        self.web_app_data = SimpleNamespace(data=json.dumps(payload, ensure_ascii=False))

    async def answer(self, text: str, **kwargs):
        return await self.bot.send_message(chat_id=self.chat.id, text=text, **kwargs)

    async def answer_document(self, document, caption: str | None = None, **kwargs):
        return await self.bot.send_document(chat_id=self.chat.id, document=document, caption=caption, **kwargs)


def _verify_mini_app_init_data(init_data: str, bot_token: str):
    """Проверяет подпись Telegram WebApp initData и возвращает пользователя."""

    if not init_data or len(init_data) > 8192:
        raise ValueError("Пустые или слишком длинные initData")
    pairs = parse_qsl(init_data, keep_blank_values=True)
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("В initData обнаружены повторяющиеся поля")
    received_hash = next((value for key, value in pairs if key == "hash"), "")
    if len(received_hash) != 64 or any(character not in "0123456789abcdefABCDEF" for character in received_hash):
        raise ValueError("В initData отсутствует подпись Telegram")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs) if key != "hash")
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise ValueError("Недействительная подпись Telegram")
    values = dict(pairs)
    try:
        auth_date = int(values["auth_date"])
        user_data = json.loads(values["user"])
        if not isinstance(user_data, dict):
            raise ValueError("Данные пользователя Telegram имеют некорректный формат")
        user_id = int(user_data["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("В initData отсутствуют данные пользователя Telegram") from exc
    now = int(datetime.now(UTC).timestamp())
    configured_max_age = _positive_env_int("MINI_APP_INIT_DATA_MAX_AGE_SECONDS", 600)
    hard_max_age = _positive_env_int("MINI_APP_INIT_DATA_HARD_MAX_AGE_SECONDS", 900)
    max_age = min(configured_max_age, hard_max_age)
    clock_skew = min(_positive_env_int("MINI_APP_INIT_DATA_CLOCK_SKEW_SECONDS", 60), 300)
    if auth_date > now + clock_skew or now - auth_date > max_age:
        raise ValueError("Срок действия initData Telegram истёк")
    username = user_data.get("username")
    signed_chat_id = None
    raw_chat = values.get("chat")
    if raw_chat:
        try:
            chat_data = json.loads(raw_chat)
            if isinstance(chat_data, dict) and "id" in chat_data:
                signed_chat_id = int(chat_data["id"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Данные чата Telegram имеют некорректный формат") from exc
    return SimpleNamespace(
        id=user_id,
        username=username if isinstance(username, str) else None,
        signed_chat_id=signed_chat_id,
        chat_type=values.get("chat_type") if isinstance(values.get("chat_type"), str) else None,
        chat_instance=values.get("chat_instance") if isinstance(values.get("chat_instance"), str) else None,
    )


def _validate_mini_app_payload(payload: object, *, require_context: bool = False) -> dict:
    """Validate and normalize the small allow-listed Mini App command schema."""

    if not isinstance(payload, dict):
        raise ValueError("Некорректное тело запроса Mini App")
    allowed_fields = {
        "action",
        "query",
        "queries",
        "email",
        "job_id",
        "shared_list_id",
        "context",
        "request_id",
        "chat_id",
    }
    unknown_fields = set(payload) - allowed_fields
    if unknown_fields:
        raise ValueError("Mini App передал неизвестные поля")
    action = payload.get("action")
    if not isinstance(action, str) or action not in MINI_APP_ACTIONS:
        raise ValueError("Неизвестное действие Mini App")
    normalized: dict = {"action": action}

    if action in {"check", "watch", "unwatch"}:
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Mini App не передал запрос компании")
        query = " ".join(query.split())
        if len(query) > MINI_APP_MAX_QUERY_LENGTH:
            raise ValueError("Запрос компании слишком длинный")
        normalized["query"] = query
    elif action in {"export_excel", "share_list"}:
        raw_queries = payload.get("queries")
        if not isinstance(raw_queries, list) or len(raw_queries) > 100:
            raise ValueError("Mini App передал некорректный список компаний")
        queries: list[str] = []
        seen: set[str] = set()
        for raw_query in raw_queries:
            if not isinstance(raw_query, str):
                raise ValueError("Mini App передал некорректный список компаний")
            query = " ".join(raw_query.split())
            if not query or len(query) > MINI_APP_MAX_QUERY_LENGTH:
                raise ValueError("Одна из записей списка компаний некорректна")
            key = query.casefold()
            if key not in seen:
                seen.add(key)
                queries.append(query)
        if len(json.dumps(queries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MINI_APP_MAX_QUERIES_PAYLOAD_BYTES:
            raise ValueError("Список компаний слишком велик для передачи одним запросом")
        normalized["queries"] = queries
    elif action == "set_email":
        email = payload.get("email")
        if not isinstance(email, str) or not email.strip() or len(email.strip()) > MINI_APP_MAX_EMAIL_LENGTH:
            raise ValueError("Адрес электронной почты некорректен")
        normalized["email"] = email.strip()
    elif action in {"batch_cancel", "batch_status"}:
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id.strip()) > MINI_APP_MAX_JOB_ID_LENGTH:
            raise ValueError("Идентификатор задания некорректен")
        normalized["job_id"] = job_id.strip()
    elif action == "import_shared_list":
        shared_list_id = payload.get("shared_list_id")
        if (
            not isinstance(shared_list_id, str)
            or not shared_list_id.strip()
            or len(shared_list_id.strip()) > MINI_APP_MAX_SHARED_LIST_ID_LENGTH
        ):
            raise ValueError("Идентификатор общего списка некорректен")
        normalized["shared_list_id"] = shared_list_id.strip()

    if "chat_id" in payload:
        try:
            normalized["chat_id"] = int(payload["chat_id"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Идентификатор чата некорректен") from exc
    if "context" in payload:
        context = payload["context"]
        if not isinstance(context, str) or not context or len(context) > 256:
            raise ValueError("Контекст Mini App некорректен")
        normalized["context"] = context
    if "request_id" in payload:
        request_id = payload["request_id"]
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("Идентификатор запроса некорректен")
        normalized["request_id"] = request_id
    if require_context and ("context" not in normalized or "request_id" not in normalized):
        raise MiniAppSecurityError("Запрос не содержит защищённого контекста Mini App")
    if not require_context and len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MINI_APP_SEND_DATA_MAX_BYTES:
        raise ValueError("Данные Mini App слишком велики для Telegram")
    return normalized


def _mini_app_allowed_origin() -> str:
    configured = os.getenv("MINI_APP_ALLOWED_ORIGIN", "").strip().rstrip("/")
    if configured:
        return configured
    parsed = urlparse(_mini_app_url())
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


@web.middleware
async def _mini_app_cors_middleware(request: web.Request, handler):
    origin = request.headers.get("Origin", "")
    allowed_origin = _mini_app_allowed_origin()
    if origin and origin != allowed_origin:
        return web.json_response({"ok": False, "error": "Недопустимый источник запроса."}, status=403)
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    if origin and origin == allowed_origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Vary"] = "Origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


async def _mini_app_api(request: web.Request) -> web.Response:
    try:
        user = _verify_mini_app_init_data(
            request.headers.get("X-Telegram-Init-Data", ""), request.app[_MINI_APP_BOT_TOKEN_KEY]
        )
        payload = _validate_mini_app_payload(await request.json(), require_context=True)
        context_token = payload.pop("context")
        request_id = payload.pop("request_id")
        raw_chat_id = payload.pop("chat_id", None)
        context = request.app[_MINI_APP_CONTEXT_STORE_KEY].resolve(context_token)
        if raw_chat_id is not None and raw_chat_id != context.chat_id:
            raise MiniAppSecurityError("Контекст чата не совпадает с launch-контекстом")
        if user.signed_chat_id is not None and user.signed_chat_id != context.chat_id:
            raise MiniAppSecurityError("Подписанный Telegram-контекст чата не совпадает")
        action = payload["action"]
        limiter: MiniAppRateLimiter = request.app[_MINI_APP_RATE_LIMITER_KEY]
        rate_keys = (
            f"user:{user.id}",
            f"user:{user.id}:chat:{context.chat_id}",
        )
        if not limiter.allow_all(*rate_keys):
            response = web.json_response(
                {"ok": False, "error": "Слишком много запросов. Повторите попытку позже."}, status=429
            )
            response.headers["Retry-After"] = str(limiter.window_seconds)
            return response
        if action in MINI_APP_EXPENSIVE_ACTIONS:
            expensive_limiter: MiniAppRateLimiter = request.app[_MINI_APP_EXPENSIVE_RATE_LIMITER_KEY]
            if not expensive_limiter.allow_all(*rate_keys):
                response = web.json_response(
                    {"ok": False, "error": "Слишком много ресурсоёмких операций. Повторите попытку позже."},
                    status=429,
                )
                response.headers["Retry-After"] = str(expensive_limiter.window_seconds)
                return response
        if not request.app[_MINI_APP_CONTEXT_STORE_KEY].claim_request(context_token, request_id):
            return web.json_response(
                {"ok": False, "error": "Этот запрос уже обработан или устарел."}, status=409
            )
    except MiniAppSecurityError as exc:
        logging.warning("Отклонён небезопасный запрос Mini App: %s", exc)
        return web.json_response({"ok": False, "error": "Контекст Mini App недействителен или устарел."}, status=401)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    if payload["action"] == "import_shared_list":
        if shared_list_store is None:
            return web.json_response({"ok": False, "error": "Общие списки сейчас недоступны."}, status=503)
        shared = shared_list_store.get(payload["shared_list_id"], context.chat_id)
        if shared is None:
            return web.json_response({"ok": False, "error": "Список устарел или недоступен."}, status=404)
        if not _can_check_user(context.chat_id, user):
            return web.json_response(
                {"ok": False, "error": "У вас нет разрешения на проверки в этом чате."}, status=403
            )
        if str(user.id) == shared.owner_id:
            return web.json_response({"ok": False, "error": "Этот список уже принадлежит вам."}, status=409)
        return web.json_response(
            {
                "ok": True,
                "shared_queries": list(shared.queries),
                "message": "Список получен и добавляется в ваш Mini App.",
            }
        )
    try:
        message = _MiniAppMessage(
            request.app[_MINI_APP_BOT_KEY], context.chat_id, context.chat_type, user, payload
        )
        async with request.app[_MINI_APP_SEMAPHORE_KEY]:
            await mini_app_data(message)
    except Exception:
        logging.exception("Ошибка обработки команды Mini App через API")
        return web.json_response({"ok": False, "error": "Команда не обработана сервером."}, status=500)
    return web.json_response({"ok": True, "message": "Команда передана боту. Ответ появится в чате."})


async def _start_mini_app_api(bot: Bot, bot_token: str):
    """Запускает локальный API, если для Mini App задан публичный HTTPS-адрес."""

    if not _mini_app_api_url():
        return None
    max_body_bytes = min(_positive_env_int("MINI_APP_API_MAX_BODY_BYTES", 32 * 1024), 128 * 1024)
    app = web.Application(client_max_size=max_body_bytes, middlewares=[_mini_app_cors_middleware])
    app[_MINI_APP_BOT_KEY] = bot
    app[_MINI_APP_BOT_TOKEN_KEY] = bot_token
    app[_MINI_APP_CONTEXT_STORE_KEY] = mini_app_context_store
    app[_MINI_APP_RATE_LIMITER_KEY] = MiniAppRateLimiter(
        limit=_positive_env_int("MINI_APP_RATE_LIMIT_PER_MINUTE", 30),
        window_seconds=_positive_env_int("MINI_APP_RATE_LIMIT_WINDOW_SECONDS", 60),
    )
    app[_MINI_APP_EXPENSIVE_RATE_LIMITER_KEY] = MiniAppRateLimiter(
        limit=_positive_env_int("MINI_APP_EXPENSIVE_RATE_LIMIT_PER_MINUTE", 8),
        window_seconds=_positive_env_int("MINI_APP_RATE_LIMIT_WINDOW_SECONDS", 60),
    )
    app[_MINI_APP_SEMAPHORE_KEY] = asyncio.Semaphore(_positive_env_int("MINI_APP_MAX_CONCURRENT_REQUESTS", 8))
    app.router.add_post(MINI_APP_API_PATH, _mini_app_api)
    app.router.add_options(MINI_APP_API_PATH, _mini_app_api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=os.getenv("MINI_APP_API_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=_positive_env_int("MINI_APP_API_PORT", 8081),
    )
    try:
        await site.start()
    except Exception:
        await runner.cleanup()
        raise
    logging.info("Mini App API запущен на %s:%s", site._host, site._port)
    return runner


async def _open_check_mini_app(message: Message) -> None:
    # Используем HTTPS API и в личном чате, если он настроен. Это сохраняет
    # Mini App открытым после отправки действия; старый sendData()-сценарий
    # оставляем резервным вариантом для установок без API.
    if _mini_app_api_url():
        keyboard = _mini_app_keyboard(chat_id=message.chat.id, chat_type=message.chat.type)
    elif message.chat.type == "private":
        keyboard = _mini_app_reply_keyboard(chat_id=message.chat.id, chat_type=message.chat.type)
    else:
        keyboard = _mini_app_keyboard(chat_id=message.chat.id, chat_type=message.chat.type)
    if keyboard is None:
        await message.answer("Mini App пока не опубликован: задайте MINI_APP_URL.")
        return
    await message.answer(
        "Откройте MiniApp и введите реквизиты компании для проверки:",
        reply_markup=keyboard,
    )


def bot_menu_keyboard() -> ReplyKeyboardMarkup:
    """Возвращает постоянное меню быстрых действий Telegram-бота."""

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🔎 Проверить компанию"),
                KeyboardButton(text="📋 История проверок"),
            ],
            [
                KeyboardButton(text="📊 Проверить все"),
                KeyboardButton(text="📁 Загрузить CSV/XLSX"),
            ],
            [
                KeyboardButton(text="👁 Мониторинг"),
                KeyboardButton(text="📄 Лицензии"),
            ],
            [
                KeyboardButton(text="🆘 Помощь"),
                KeyboardButton(text="🧠 Навыки"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие или напишите запрос",
    )


def _allowed(message: Message) -> bool:
    return bool(message.from_user and _allowed_user(message.chat.id, message.from_user))


def _can_check(message: Message) -> bool:
    return bool(message.from_user and _can_check_user(message.chat.id, message.from_user))


def _can_check_user(chat_id: int, user) -> bool:
    if not access_store:
        return False
    role = access_store.role_for(chat_id, user_id=user.id, username=user.username)
    return role in {"owner", "manager", "reviewer", "checker"}


def _allowed_user(chat_id: int, user) -> bool:
    return bool(
        access_store
        and access_store.is_allowed(
            chat_id,
            user_id=user.id,
            username=user.username,
        )
    )


def _is_root(message: Message) -> bool:
    return bool(
        message.from_user
        and is_main_admin(message.from_user.username, user_id=message.from_user.id)
    )


async def _react_to_message(message: Message, bot: Bot) -> None:
    """Ставит настроенную реакцию на сообщение доверенного пользователя.

    Неудача с реакцией не должна блокировать обработку команды или диалога:
    например, в чате может быть запрещено право бота реагировать на сообщения.
    """

    if not reaction_settings.enabled or not message.from_user or message.from_user.is_bot or not _allowed(message):
        return
    reaction_label = reaction_settings.custom_emoji_id or reaction_settings.choose_emoji()
    try:
        reaction = (
            ReactionTypeCustomEmoji(custom_emoji_id=reaction_settings.custom_emoji_id)
            if reaction_settings.custom_emoji_id
            else ReactionTypeEmoji(emoji=reaction_label)
        )
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[reaction],
            is_big=False,
        )
    except Exception:
        logging.warning(
            "Не удалось поставить реакцию %s в чате %s на сообщение %s",
            reaction_label,
            message.chat.id,
            message.message_id,
            exc_info=True,
        )


@router.message.outer_middleware()
async def reaction_middleware(handler, event, data):
    """Добавляет реакцию до передачи сообщения профильному обработчику."""

    bot = data.get("bot")
    if isinstance(event, Message) and bot is not None:
        await _react_to_message(event, bot)
    return await handler(event, data)


def help_text() -> str:
    """Возвращает единый список команд для справки и документации бота."""

    return (
        "Команды бота:\n\n"
        "/start — запустить бота и получить краткую инструкцию.\n"
        "/help — показать этот список команд.\n"
        "/menu — показать постоянное меню быстрых действий.\n"
        "/skills — рассказать, что умеет агент.\n"
        "/check [реквизиты] — проверить в чате или открыть MiniApp без аргументов.\n"
        "  Введите ИНН, ОГРН, КПП, название или адрес; без аргументов откроется MiniApp.\n"
        "  После проверки бот отправляет текст и Excel-файл с четырьмя столбцами.\n"
        "  Для запуска проверки напишите: «Налог, проверка».\n"
        "/history — показать ранее выполненные проверки в текущем чате.\n"
        "/history ID — показать сохранённый итог конкретной проверки.\n"
        "/why ID — показать результат, источники, время и покрытие проверки.\n"
        "/license ИНН — проверить сохранённые сведения о лицензии компании.\n"
        "/license_add НОМЕР ИНН ДД.ММ.ГГГГ — добавить срок лицензии (администратор).\n"
        "/license_list — список лицензий и сроков.\n"
        "/license_monitor [дней] — лицензии, истекающие в указанный срок.\n"
        "/watch реквизиты — добавить компанию в список мониторинга.\n"
        "/unwatch реквизиты — убрать компанию из списка мониторинга.\n"
        "/watched — показать список компаний для мониторинга.\n"
        "Настройка email-уведомлений доступна в Mini App.\n"
        "/app — открыть Telegram Mini App (если задан MINI_APP_URL).\n"
        "/check_all (или /check all) — повторно проверить все уникальные юрлица из базы истории (администратор).\n"
        "/batch_status — показать состояние текущей массовой проверки.\n"
        "/batch_cancel ID — отменить текущую массовую проверку.\n"
        "/feedback ID ОЦЕНКА — отправить оценку результата проверки.\n"
        "  Оценки: correct, incorrect или needs_review.\n\n"
        "/chat_reset — очистить память разговорного диалога в текущем чате.\n\n"
        "Реакции бота на сообщения доверенных пользователей настраиваются через REACTIONS_ENABLED, REACTION_EMOJIS и REACTION_MODE.\n\n"
        "Команды Главного администратора:\n"
        "/grant @username [viewer|checker|reviewer|manager] — выдать доступ.\n"
        "/revoke @username — отозвать доступ пользователя в текущем чате.\n"
        "/trusted — показать доверенных пользователей текущего чата.\n"
        "/audit — журнал действий в текущем чате (администратор).\n"
        "/learning_queue — показать очередь обратной связи.\n"
        "/learning_review ID approved|rejected — проверить пример обучения.\n"
        "/learning_export — экспортировать одобренные примеры обучения."
    )


def skills_text() -> str:
    """Краткое описание прикладных возможностей агента для пользователя."""
    return (
        "Навыки агента «Налог»:\n\n"
        "🔎 Проверка юридических лиц — поиск по ИНН, ОГРН, КПП, названию, адресу и свободному описанию.\n"
        "⚠️ Недостоверность — определение отметок ФНС с разделением на «есть», «нет» и «не распознано».\n"
        "📄 Лицензии — проверка сохранённых сведений, сроков действия и приближения даты окончания.\n"
        "📊 Мониторинг — повторные проверки компаний и уведомления об изменениях.\n"
        "✉️ Email-уведомления — письма при появлении отметки о недостоверности.\n"
        "📁 Массовые проверки — обработка CSV/XLSX и Excel-отчёт по результатам.\n"
        "🧾 Объяснение результата — команда /why показывает источник, время, маркеры и основания вывода.\n"
        "🗂 История — сохранение результатов и повторный просмотр предыдущих проверок.\n"
        "💬 Русский диалог — обработка обращений, начинающихся с «Налог».\n"
        "🧠 Контролируемое обучение — обратная связь пользователей с проверкой администратором.\n"
        "👥 Доступы — проверка доверенных пользователей, роли и отдельные права в каждом чате.\n"
        "📋 Аудит — журнал административных действий и операций.\n"
        "🖥 Mini App — таблица компаний, запуск проверки и добавление в мониторинг.\n\n"
        "Для команд используйте /help. Запуск проверки:\n"
        "«Налог, проверка»."
    )


@router.message(CommandStart())
async def start(message: Message, bot: Bot) -> None:
    url = _mini_app_link(chat_id=message.chat.id, chat_type=message.chat.type)
    if url:
        try:
            await bot.set_chat_menu_button(
                chat_id=message.chat.id,
                menu_button=MenuButtonWebApp(text="Налог", web_app=WebAppInfo(url=url)),
            )
        except Exception:
            logging.warning("Не удалось установить кнопку Mini App для чата %s", message.chat.id, exc_info=True)
    await message.answer(
        "Агент готов. Для начала проверки напишите: «Налог, проверка».\n"
        "Реквизиты компании вводятся внутри MiniApp.\n"
        "После ответа можно поставить оценку кнопками обратной связи.\n"
        "Для полного списка команд используйте /help.",
        reply_markup=bot_menu_keyboard(),
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(help_text())


@router.message(Command("skills", "about"))
async def skills_command(message: Message) -> None:
    await message.answer(skills_text())


@router.message(Command("menu"))
async def menu_command(message: Message) -> None:
    await message.answer("Быстрые действия бота:", reply_markup=bot_menu_keyboard())


@router.message(Command("app"))
async def app_command(message: Message, bot: Bot) -> None:
    url = _mini_app_link(chat_id=message.chat.id, chat_type=message.chat.type)
    if not url:
        await message.answer("Mini App пока не опубликован: задайте MINI_APP_URL.")
        return
    try:
        await bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(text="Налог", web_app=WebAppInfo(url=url)),
        )
    except Exception:
        logging.warning("Не удалось установить кнопку Mini App для чата %s", message.chat.id, exc_info=True)
    await message.answer(
        "Откройте панель проверки:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Открыть Mini App", web_app=WebAppInfo(url=url)),
                ]
            ]
        ),
    )


@router.message(Command("chat_reset"))
async def chat_reset(message: Message) -> None:
    if not _allowed(message) or not message.from_user:
        await message.answer("У вас нет разрешения на разговорный режим.")
        return
    if chat_skill is None:
        await message.answer("Разговорный режим не настроен.")
        return
    chat_skill.clear(f"{message.chat.id}:{message.from_user.id}")
    await message.answer("Память разговорного диалога очищена.")


def _feedback_keyboard(event_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Верно", callback_data=f"feedback:{event_id}:correct"),
                InlineKeyboardButton(text="❌ Ошибка", callback_data=f"feedback:{event_id}:incorrect"),
                InlineKeyboardButton(text="🔎 Проверить", callback_data=f"feedback:{event_id}:needs_review"),
            ]
        ]
    )


@router.message(Command("check"))
async def check(message: Message, command: CommandObject) -> None:
    if command.args and command.args.strip().casefold() == "all":
        await check_all(message)
        return
    if command.args and command.args.strip():
        await _run_check(message, command.args.strip())
        return
    await _open_check_mini_app(message)


async def _run_check(message: Message, raw_query: str) -> None:
    if not _can_check(message):
        await message.answer("У вас нет разрешения на выполнение этой команды.")
        return
    if not raw_query.strip():
        await message.answer("Формат: /check ИНН 7707083893 КПП 770401001")
        return
    if agent is None:
        await message.answer("Проверка сейчас недоступна: агент не настроен.")
        return
    mini_app_keyboard = _mini_app_keyboard(chat_id=message.chat.id, chat_type=message.chat.type)
    if mini_app_keyboard:
        await message.answer(
            "Для проверки и дальнейшего управления результатом можно открыть Mini App:",
            reply_markup=mini_app_keyboard,
        )
    query = None
    try:
        query = parse_search_query(raw_query)
        result = await agent.check(query)
    except InvalidIdentifier as exc:
        await message.answer(f"Не удалось распознать реквизиты: {exc}")
    except FnsError as exc:
        if learning_store and message.from_user and query is not None:
            try:
                learning_store.record_failure(
                    chat_id=message.chat.id,
                    actor_id=str(message.from_user.id),
                    identifier=query,
                    error=str(exc),
                )
            except Exception:
                logging.exception("Не удалось сохранить ошибку проверки в журнал обучения")
        await message.answer(f"Проверка ФНС не выполнена: {exc}")
    except Exception:
        logging.exception("Непредвиденная ошибка одиночной проверки")
        if learning_store and message.from_user and query is not None:
            try:
                learning_store.record_failure(
                    chat_id=message.chat.id,
                    actor_id=str(message.from_user.id), identifier=query, error="internal error"
                )
            except Exception:
                logging.exception("Не удалось сохранить внутреннюю ошибку в журнал обучения")
        await message.answer("Проверка не выполнена из-за внутренней ошибки. Попробуйте ещё раз.")
    else:
        report = format_assessment(result)
        _audit(message, "check_completed", "result=success")
        history_event_id = secrets.token_urlsafe(9)
        learning_event_id = None
        if learning_store and message.from_user:
            try:
                learning_event_id = learning_store.record_check(
                    chat_id=message.chat.id,
                    actor_id=str(message.from_user.id),
                    identifier=query,
                    assessment=result,
                    rendered_response=report,
                )
                history_event_id = learning_event_id
            except Exception:
                logging.exception("Не удалось сохранить результат в журнал обучения")
        if history_store and message.from_user:
            try:
                history_store.record_check(
                    event_id=history_event_id,
                    chat_id=message.chat.id,
                    actor_id=message.from_user.id,
                    query=query,
                    assessment=result,
                    report=report,
                )
            except Exception:
                logging.exception("Не удалось сохранить результат в историю проверок")
        suffix = "\n\nОцените результат, чтобы агент мог улучшаться после проверки администратора."
        await message.answer(
            report + suffix,
            reply_markup=_feedback_keyboard(learning_event_id) if learning_event_id else None,
        )
        try:
            filename_id = history_event_id or "result"
            await message.answer_document(
                BufferedInputFile(
                    build_check_workbook([row_from_assessment(result)]),
                    filename=f"check_{filename_id}.xlsx",
                ),
                caption="Результат проверки в формате Excel.",
            )
        except Exception:
            logging.exception("Не удалось отправить Excel-отчёт одиночной проверки")
            await message.answer("Текстовый результат готов, но Excel-файл отправить не удалось.")


@router.callback_query(F.data.startswith("feedback:"))
async def feedback_callback(callback: CallbackQuery) -> None:
    if (
        not callback.from_user
        or not callback.message
        or not _allowed_user(callback.message.chat.id, callback.from_user)
    ):
        await callback.answer("Нет разрешения на отправку обратной связи.", show_alert=True)
        return
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or learning_store is None:
        await callback.answer("Не удалось распознать обратную связь.", show_alert=True)
        return
    try:
        label = FeedbackLabel(parts[2])
    except ValueError:
        await callback.answer("Неизвестная оценка.", show_alert=True)
        return
    saved = learning_store.add_feedback(
        event_id=parts[1],
        chat_id=callback.message.chat.id,
        actor_id=str(callback.from_user.id),
        label=label,
        is_admin=is_main_admin(callback.from_user.username, user_id=callback.from_user.id),
    )
    _audit(callback.message, "feedback_added", f"label={label.value}")
    await callback.answer("Спасибо, оценка сохранена." if saved else "Запись не найдена.", show_alert=not saved)


@router.message(Command("feedback"))
async def feedback_command(message: Message, command: CommandObject) -> None:
    if not _allowed(message) or not message.from_user or learning_store is None:
        await message.answer("У вас нет разрешения на отправку обратной связи.")
        return
    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Формат: /feedback ID correct|incorrect|needs_review [комментарий]")
        return
    try:
        label = FeedbackLabel(parts[1])
    except ValueError:
        await message.answer("Оценка: correct, incorrect или needs_review.")
        return
    saved = learning_store.add_feedback(
        event_id=parts[0],
        chat_id=message.chat.id,
        actor_id=str(message.from_user.id),
        label=label,
        note=parts[2] if len(parts) == 3 else None,
        is_admin=is_main_admin(message.from_user.username, user_id=message.from_user.id),
    )
    _audit(message, "feedback_added", f"label={label.value}")
    await message.answer("Обратная связь сохранена." if saved else "Запись не найдена или недоступна.")


def _history_line(entry: HistoryEntry) -> str:
    title = entry.name or entry.query_text
    title = title if len(title) <= 70 else title[:67] + "..."
    identifiers = " / ".join(value for value in (entry.inn, entry.kpp, entry.ogrn) if value)
    state = {
        "present": "есть отметка о недостоверности",
        "absent": "отметка о недостоверности отсутствует",
        "not_reported": "не распознано",
    }.get(entry.inaccuracy_state, entry.inaccuracy_state)
    details = f"; {identifiers}" if identifiers else ""
    return f"{entry.event_id} — {entry.created_at:%d.%m.%Y %H:%M}; {title}{details}; {state}"


def _history_excel_row(entry: HistoryEntry) -> ExcelCheckRow:
    identifiers = "; ".join(
        value
        for value in (
            f"ИНН: {entry.inn}" if entry.inn else None,
            f"ОГРН: {entry.ogrn}" if entry.ogrn else None,
            f"КПП: {entry.kpp}" if entry.kpp else None,
            f"Запрос: {entry.query_text}" if entry.query_text else None,
        )
        if value
    )
    inaccuracy = {
        "present": "Да",
        "absent": "Нет",
        "not_reported": "Не распознано",
    }.get(entry.inaccuracy_state, entry.inaccuracy_state)
    return ExcelCheckRow(
        name=entry.name or "—",
        lookup=identifiers or "—",
        inaccuracy=inaccuracy,
    )


async def _export_mini_app_excel(message: Message, raw_queries: list[str]) -> None:
    if not _allowed(message) or not message.from_user or history_store is None:
        await message.answer("У вас нет разрешения выгружать результаты проверок.")
        return
    queries = list(dict.fromkeys(query.strip() for query in raw_queries if query.strip()))
    if not queries:
        await message.answer("Список компаний пуст.")
        return
    if len(queries) > 100:
        await message.answer("За один раз можно выгрузить не более 100 компаний.")
        return
    entries = history_store.list_for(
        chat_id=message.chat.id,
        actor_id=message.from_user.id,
        is_root=False,
        limit=100,
    )
    latest_by_query: dict[str, HistoryEntry] = {}
    for entry in entries:
        key = " ".join(entry.query_text.split()).casefold()
        latest_by_query.setdefault(key, entry)
    rows: list[ExcelCheckRow] = []
    for query in queries:
        key = " ".join(query.split()).casefold()
        rows.append(
            _history_excel_row(latest_by_query[key])
            if key in latest_by_query
            else row_from_error(query, "результат проверки отсутствует")
        )
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    try:
        await message.answer_document(
            BufferedInputFile(
                build_check_workbook(rows),
                filename=f"mini_app_checks_{timestamp}.xlsx",
            ),
            caption="Результаты компаний из списка MiniApp в формате Excel.",
        )
        _audit(message, "mini_app_excel_export", f"count={len(queries)}")
    except Exception:
        logging.exception("Не удалось отправить Excel-выгрузку MiniApp")
        await message.answer("Не удалось сформировать Excel-файл. Попробуйте ещё раз.")


@router.message(Command("history"))
async def history(message: Message, command: CommandObject) -> None:
    await _show_history(message, (command.args or "").strip())


async def _show_history(message: Message, event_id: str = "") -> None:
    if not _allowed(message) or not message.from_user or history_store is None:
        await message.answer("У вас нет разрешения на просмотр истории проверок.")
        return
    if event_id:
        entry = history_store.get_for(
            event_id=event_id,
            chat_id=message.chat.id,
            actor_id=message.from_user.id,
            is_root=_is_root(message),
        )
        await message.answer(entry.report if entry else "Проверка не найдена или недоступна.")
        return
    entries = history_store.list_for(
        chat_id=message.chat.id,
        actor_id=message.from_user.id,
        is_root=_is_root(message),
    )
    if not entries:
        await message.answer("Сохранённых проверок в этом чате нет.")
        return
    header = "История проверок текущего чата:\n" if _is_root(message) else "Ваши проверки в текущем чате:\n"
    await message.answer(header + "\n".join(_history_line(entry) for entry in entries))


@router.message(Command("why"))
async def why_command(message: Message, command: CommandObject) -> None:
    """Показывает объяснимый результат без повторного сетевого запроса."""
    if not _allowed(message) or not message.from_user or history_store is None:
        await message.answer("У вас нет разрешения на просмотр результата.")
        return
    event_id = (command.args or "").strip()
    if not event_id:
        await message.answer("Формат: /why ID проверки. ID можно найти в /history.")
        return
    entry = history_store.get_for(
        event_id=event_id, chat_id=message.chat.id, actor_id=message.from_user.id, is_root=_is_root(message)
    )
    if entry is None:
        await message.answer("Проверка не найдена или недоступна.")
        return
    await message.answer(
        f"Объяснение проверки {entry.event_id}\n"
        f"Запрос: {entry.query_text}\n"
        f"Проверено: {entry.created_at:%d.%m.%Y %H:%M UTC}\n"
        f"Источник: {entry.source_url}\n"
        f"Статус недостоверности: {entry.inaccuracy_state}\n"
        f"Маркеры: {', '.join(entry.inaccuracy_markers) or 'не указаны'}\n\n"
        f"Полный результат:\n{entry.report}"
    )


def _license_line(record) -> str:
    left = f"{record.name or record.inn} — {record.license_type}; № {record.license_id}"
    expiry = record.expires_at.strftime("%d.%m.%Y") if record.expires_at else "не указан"
    days = record.days_left()
    suffix = f"; {record.state()}" + (f" ({days} дн.)" if days is not None else "")
    source = f"; источник: {record.source_url}" if record.source_url else ""
    return left + f"; до {expiry}" + suffix + source


@router.message(Command("license"))
async def license_command(message: Message, command: CommandObject) -> None:
    if not _can_check(message) or license_store is None:
        await message.answer("У вас нет разрешения на проверку лицензий.")
        return
    raw = (command.args or "").strip()
    if not raw:
        await message.answer("Формат: /license ИНН 7707083893 или /license ОГРН …")
        return
    try:
        query = parse_search_query(raw)
    except InvalidIdentifier as exc:
        await message.answer(f"Не удалось распознать реквизиты: {exc}")
        return
    inns = [item.value for item in query.identifiers if item.kind.value == "ИНН"]
    entity_report = ""
    if agent is not None:
        try:
            result = await agent.check(query)
            inns = [result.record.inn] if result.record.inn else []
            entity_report = format_assessment(result)
        except FnsError as exc:
            await message.answer(f"Сначала не удалось проверить юридическое лицо по ФНС: {exc}")
            return
        except Exception:
            logging.exception("Не удалось определить ИНН для проверки лицензии")
    if not inns:
        await message.answer("Для проверки лицензии нужен ИНН или однозначно найденная компания.")
        return
    records = license_store.for_inn(inns[0])
    if not records:
        await message.answer(
            "В локальном реестре лицензий запись не найдена. Это не означает, что лицензии нет: "
            "подключите официальный источник/добавьте подтверждённую запись через /license_add."
        )
        return
    prefix = (entity_report + "\n\n") if entity_report else ""
    await message.answer(prefix + "Лицензии компании:\n" + "\n".join(_license_line(item) for item in records))


@router.message(Command("license_add"))
async def license_add_command(message: Message, command: CommandObject) -> None:
    if not _is_root(message) or license_store is None:
        await message.answer("Команда доступна Главному администратору.")
        return
    parts = (command.args or "").split(maxsplit=3)
    if len(parts) < 3:
        await message.answer("Формат: /license_add НОМЕР ИНН ДД.ММ.ГГГГ [тип или примечание]")
        return
    try:
        expires = datetime.strptime(parts[2], "%d.%m.%Y").date()
    except ValueError:
        await message.answer("Дата должна быть в формате ДД.ММ.ГГГГ.")
        return
    record = new_license(
        license_id=parts[0], inn=parts[1], expires_at=expires, notes=parts[3] if len(parts) == 4 else None
    )
    license_store.upsert(record)
    _audit(message, "license_add", "license_record_created")
    await message.answer(f"Лицензия {parts[0]} сохранена до {expires:%d.%m.%Y}.")


@router.message(Command("license_list"))
async def license_list_command(message: Message) -> None:
    if not _allowed(message) or license_store is None:
        await message.answer("У вас нет разрешения на просмотр лицензий.")
        return
    records = license_store.all()
    await message.answer(
        "Лицензий в реестре нет." if not records else "\n".join(_license_line(item) for item in records)
    )


@router.message(Command("license_monitor"))
async def license_monitor_command(message: Message, command: CommandObject) -> None:
    if not _allowed(message) or license_store is None:
        await message.answer("У вас нет разрешения на мониторинг лицензий.")
        return
    try:
        days = int((command.args or "30").strip())
    except ValueError:
        days = 30
    days = max(1, min(days, 3650))
    records = license_store.expiring(days)
    if not records:
        await message.answer(f"Лицензий, истекающих в ближайшие {days} дней, не найдено.")
        return
    await message.answer(f"Истекают в ближайшие {days} дней:\n" + "\n".join(_license_line(item) for item in records))


@router.message(Command("audit"))
async def audit_command(message: Message) -> None:
    if not _is_root(message) or audit_store is None:
        await message.answer("Команда доступна Главному администратору.")
        return
    rows = audit_store.recent(message.chat.id)
    if not rows:
        await message.answer("Журнал действий пуст.")
        return
    await message.answer(
        "Журнал действий:\n"
        + "\n".join(f"{row['created_at'][:19]} — {row['action']} — {row['details']}" for row in rows)
    )


@router.message(Command("watch"))
async def watch_command(message: Message, command: CommandObject) -> None:
    if not _allowed(message) or not message.from_user or watch_store is None:
        await message.answer("У вас нет разрешения на мониторинг компаний.")
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer("Формат: /watch ИНН 7707083893")
        return
    watch_store.add(message.chat.id, query, message.from_user.id)
    _audit(message, "watch_added", "company_added")
    await message.answer(
        "Компания добавлена в список мониторинга. Периодический планировщик подключим после настройки интервала и канала уведомлений."
    )


@router.message(Command("unwatch"))
async def unwatch_command(message: Message, command: CommandObject) -> None:
    if not _allowed(message) or watch_store is None:
        await message.answer("У вас нет разрешения на изменение мониторинга.")
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer("Формат: /unwatch ИНН 7707083893")
        return
    removed = watch_store.remove(message.chat.id, query)
    if removed:
        _audit(message, "watch_removed", "company_removed")
    await message.answer(
        "Компания удалена из списка мониторинга." if removed else "Такой компании нет в списке мониторинга."
    )


@router.message(Command("watched"))
async def watched_command(message: Message) -> None:
    if not _allowed(message) or watch_store is None:
        await message.answer("У вас нет разрешения на просмотр мониторинга.")
        return
    items = watch_store.list(message.chat.id)
    await message.answer("Список мониторинга пуст." if not items else "Компании в мониторинге:\n" + "\n".join(items))


def _positive_env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logging.warning("%s должен быть целым числом; используется значение %s.", name, default)
        return default
    return parsed if parsed > 0 else default


def _positive_env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        logging.warning("%s должен быть числом; используется значение %s.", name, default)
        return default
    return parsed if parsed > 0 else default


async def _answer_chunks(message: Message, lines: list[str], *, limit: int = 3900) -> None:
    """Отправляет длинный итог несколькими сообщениями в пределах лимита Telegram."""

    chunk: list[str] = []
    length = 0
    for line in lines:
        addition = len(line) + (1 if chunk else 0)
        if chunk and length + addition > limit:
            await message.answer("\n".join(chunk))
            chunk = []
            length = 0
        chunk.append(line)
        length += len(line) + (1 if length else 0)
    if chunk:
        await message.answer("\n".join(chunk))


def _bulk_result_line(query: str, event_id: str, result) -> str:
    record = result.record
    title = record.name or query
    identifiers = " / ".join(value for value in (record.inn, record.kpp, record.ogrn) if value)
    state = {
        "present": "есть отметка о недостоверности",
        "absent": "отметка о недостоверности отсутствует",
        "not_reported": "отметка о недостоверности не распознана",
    }.get(record.inaccuracy_state.value, record.inaccuracy_state.value)
    details = f" ({identifiers})" if identifiers else ""
    return f"✅ {title}{details} — {state}; ID проверки: {event_id}"


async def _record_batch_failure(job: BatchJob, query: SearchQuery, error: str) -> None:
    if learning_store is None:
        return
    try:
        learning_store.record_failure(
            chat_id=job.chat_id,
            actor_id=str(job.actor_id),
            identifier=query,
            error=error,
        )
    except Exception:
        logging.exception("Не удалось сохранить ошибку пакетной проверки в журнал обучения")


async def _record_batch_success(message: Message, job: BatchJob, query: SearchQuery, result) -> tuple[str, str]:
    report = format_assessment(result)
    event_id = secrets.token_urlsafe(9)
    if learning_store:
        try:
            event_id = learning_store.record_check(
                chat_id=job.chat_id,
                actor_id=str(job.actor_id),
                identifier=query,
                assessment=result,
                rendered_response=report,
            )
        except Exception:
            logging.exception("Не удалось сохранить результат пакетной проверки в журнал обучения")
    if history_store:
        try:
            history_store.record_check(
                event_id=event_id,
                chat_id=message.chat.id,
                actor_id=job.actor_id,
                query=query,
                assessment=result,
                report=report,
            )
        except Exception:
            logging.exception("Не удалось сохранить результат пакетной проверки в историю")
    return event_id, report


async def _run_batch_job(job: BatchJob, message: Message) -> None:
    """Последовательно выполнить пакет и собрать единый Excel-отчёт."""

    rows = []
    succeeded = 0
    failed = 0
    try:
        while job.index < len(job.queries):
            query = job.queries[job.index]
            result = None
            attempt = 0
            while job.state is not BatchJobState.CANCELLED:
                try:
                    if agent is None:
                        raise FnsError("агент не настроен")
                    result = await agent.check(query)
                except FnsTransientError as exc:
                    retries = _positive_env_int("BATCH_RETRY_ATTEMPTS", 2)
                    if attempt < retries:
                        delay = _positive_env_float("BATCH_RETRY_DELAY_SECONDS", 3.0) * (2**attempt)
                        attempt += 1
                        await message.answer(
                            f"⏳ Временная ошибка ФНС для {query.value}. Повтор через {delay:g} с "
                            f"({attempt}/{retries})."
                        )
                        await asyncio.sleep(delay)
                        continue
                    failed += 1
                    error = str(exc)
                    await _record_batch_failure(job, query, error)
                    rows.append(row_from_error(query.value, f"проверка не выполнена: {error}"))
                    await message.answer(f"❌ {query.value} — {error}")
                    break
                except FnsNotFoundError as exc:
                    failed += 1
                    error = str(exc)
                    await _record_batch_failure(job, query, error)
                    rows.append(row_from_error(query.value, error))
                    await message.answer(f"❌ {query.value} — {error}")
                    break
                except FnsError as exc:
                    failed += 1
                    error = str(exc)
                    await _record_batch_failure(job, query, error)
                    rows.append(row_from_error(query.value, f"проверка не выполнена: {error}"))
                    await message.answer(f"❌ {query.value} — {error}")
                    break
                except Exception:
                    logging.exception("Ошибка пакетной проверки %s", query.value)
                    failed += 1
                    error = "внутренняя ошибка проверки"
                    await _record_batch_failure(job, query, error)
                    rows.append(row_from_error(query.value, error))
                    await message.answer(f"❌ {query.value} — {error}")
                    break
                else:
                    event_id, _report = await _record_batch_success(message, job, query, result)
                    succeeded += 1
                    rows.append(row_from_assessment(result))
                    await message.answer(_bulk_result_line(query.value, event_id, result))
                    break

            if job.state is BatchJobState.CANCELLED:
                break
            job.index += 1

        if job.state is BatchJobState.CANCELLED:
            remaining = job.queries[job.index :]
            rows.extend(row_from_error(query.value, "проверка отменена пользователем") for query in remaining)
            await message.answer(f"⏹ Пакетная проверка отменена. Обработано: {job.index}/{len(job.queries)}.")
        else:
            job.complete()
            await message.answer(f"✅ Массовая проверка завершена: успешно — {succeeded}, с ошибкой — {failed}.")

        _audit(
            message,
            "check_all_completed" if job.source == "history" else "bulk_completed",
            f"job={job.job_id};success={succeeded};failed={failed};state={job.state.value}",
        )
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        try:
            await message.answer_document(
                BufferedInputFile(
                    build_check_workbook(rows),
                    filename=f"batch_check_{timestamp}.xlsx",
                ),
                caption="Результаты пакетной проверки в формате Excel (4 столбца).",
            )
        except Exception:
            logging.exception("Не удалось отправить Excel-отчёт пакетной проверки")
            await message.answer("Текстовая сводка готова, но Excel-файл отправить не удалось.")
    except asyncio.CancelledError:
        job.cancel()
        raise
    finally:
        batch_tasks.pop(job.job_id, None)
        batch_jobs.remove(job.job_id)


async def _start_batch_job(message: Message, queries: list[SearchQuery], *, source: str) -> None:
    if not message.from_user:
        await message.answer("Не удалось определить пользователя, запустившего пакет.")
        return
    try:
        job = batch_jobs.create(
            chat_id=message.chat.id,
            actor_id=message.from_user.id,
            queries=queries,
            source=source,
        )
    except ValueError:
        current = batch_jobs.active_for_chat(message.chat.id)
        suffix = f" ID текущего задания: {current.job_id}" if current else ""
        await message.answer(f"В этом чате уже выполняется пакетная проверка.{suffix}")
        return
    _audit(
        message,
        "check_all_started" if source == "history" else "bulk_started",
        f"job={job.job_id};count={len(queries)}",
    )
    await message.answer(
        f"Запускаю последовательную проверку {len(queries)} компаний. "
        f"Задание: {job.job_id}. Для просмотра состояния используйте /batch_status.",
        reply_markup=_mini_app_keyboard(chat_id=message.chat.id, chat_type=message.chat.type),
    )
    task = asyncio.create_task(_run_batch_job(job, message))
    batch_tasks[job.job_id] = task


def _batch_job_for_message(message: Message, job_id: str = "") -> BatchJob | None:
    if job_id:
        job = batch_jobs.get(job_id)
    else:
        job = batch_jobs.active_for_chat(message.chat.id)
    if (
        job is None
        or not message.from_user
        or job.chat_id != message.chat.id
        or job.actor_id != message.from_user.id
        or not _can_check_user(message.chat.id, message.from_user)
    ):
        return None
    return job


@router.message(Command("batch_status"))
async def batch_status(message: Message, command: CommandObject) -> None:
    job = _batch_job_for_message(message, (command.args or "").strip())
    if job is None:
        await message.answer("Активной пакетной проверки в этом чате нет.")
        return
    current = job.current_query.value if job.current_query else "—"
    await message.answer(
        f"Задание {job.job_id}: {job.state.value}; "
        f"обработано {job.index}/{len(job.queries)}; текущий запрос: {current}."
    )


@router.message(Command("batch_cancel"))
async def batch_cancel(message: Message, command: CommandObject) -> None:
    job = _batch_job_for_message(message, (command.args or "").strip())
    if job is None:
        await message.answer("Активной пакетной проверки в этом чате нет.")
        return
    job.cancel()
    await message.answer("⏹ Запрошена отмена пакетной проверки.")


@router.message(Command("check_all", "checkall"))
async def check_all(message: Message) -> None:
    """Повторно проверяет все уникальные организации, сохранённые в истории."""

    if not _is_root(message):
        await message.answer("Команда доступна Главному администратору.")
        return
    if not message.from_user or agent is None or history_store is None:
        await message.answer("Массовая проверка сейчас недоступна: хранилище не настроено.")
        return

    queries = history_store.list_unique_queries(
        chat_id=message.chat.id,
        actor_id=message.from_user.id,
        is_root=True,
    )
    if not queries:
        await message.answer("В базе истории нет сохранённых юридических лиц для проверки.")
        return

    await _start_batch_job(message, queries, source="history")
    return


@router.message(F.document)
async def bulk_document(message: Message, bot: Bot) -> None:
    """Проверяет CSV/XLSX по подписи «/bulk» или «проверить файл»."""
    caption = (message.caption or "").casefold()
    if not any(marker in caption for marker in ("/bulk", "проверить файл", "проверь файл")):
        return
    if not _can_check(message) or agent is None or not message.document:
        await message.answer("У вас нет разрешения на массовую проверку.")
        return
    try:
        file = await bot.get_file(message.document.file_id)
        buffer = __import__("io").BytesIO()
        await bot.download_file(file.file_path, destination=buffer)
        queries = parse_upload(message.document.file_name or "upload.csv", buffer.getvalue())
    except (ValueError, StopIteration) as exc:
        await message.answer(f"Не удалось прочитать файл: {exc}")
        return
    except Exception:
        logging.exception("Ошибка загрузки массового файла")
        await message.answer("Не удалось загрузить файл. Поддерживаются CSV и XLSX.")
        return
    if not queries:
        await message.answer("В файле не найдено строк с реквизитами юридических лиц.")
        return
    max_rows = _positive_env_int("BULK_MAX_ROWS", 500)
    if len(queries) > max_rows:
        await message.answer(f"За один запуск можно проверить до {max_rows} строк.")
        return
    unique: list = []
    seen: set[str] = set()
    for query in queries:
        key = " ".join(query.value.split()).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(query)
    queries = unique
    await _start_batch_job(message, queries, source="upload")
    return


@router.message(F.web_app_data)
async def mini_app_data(message: Message) -> None:
    if not message.web_app_data:
        return
    try:
        payload = _validate_mini_app_payload(json.loads(message.web_app_data.data))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logging.info("Отклонены данные Mini App: %s", exc)
        await message.answer("Mini App передал некорректные данные.")
        return
    action = payload.get("action")
    if action == "export_excel":
        raw_queries = payload.get("queries")
        if not isinstance(raw_queries, list) or not all(isinstance(query, str) for query in raw_queries):
            await message.answer("Mini App передал некорректный список компаний.")
            return
        await _export_mini_app_excel(message, raw_queries)
        return
    if action == "share_list":
        if not _can_check(message) or not message.from_user or shared_list_store is None:
            await message.answer("У вас нет разрешения делиться списком компаний.")
            return
        raw_queries = payload.get("queries")
        if not isinstance(raw_queries, list) or not all(isinstance(query, str) for query in raw_queries):
            await message.answer("Mini App передал некорректный список компаний.")
            return
        try:
            shared = shared_list_store.create(message.chat.id, message.from_user.id, raw_queries)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        _audit(message, "company_list_shared", f"count={len(shared.queries)}")
        await message.answer(
            f"Список из {len(shared.queries)} компаний подготовлен. "
            "В этом чате другой пользователь может нажать кнопку и добавить его себе.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Добавить список себе",
                            callback_data=f"shared_list:{shared.share_id}",
                        )
                    ]
                ]
            ),
        )
        return
    if action in {"set_email", "remove_email"}:
        if not message.from_user or not _allowed(message) or notification_store is None:
            await message.answer("У вас нет разрешения на настройку email-уведомлений.")
            return
        if action == "remove_email":
            removed = notification_store.remove(message.chat.id)
            _audit(message, "email_notifications_disabled", "email_removed")
            await message.answer(
                "Email-уведомления отключены." if removed else "Для этого чата email-уведомления не были настроены."
            )
            return
        raw_email = payload.get("email")
        if not isinstance(raw_email, str) or not raw_email.strip():
            await message.answer("Укажите адрес электронной почты в Mini App.")
            return
        try:
            recipient = normalize_email(raw_email)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        notification_store.set(message.chat.id, recipient, message.from_user.id)
        _audit(message, "email_notifications_enabled", "email_saved")
        if smtp_config.configured:
            await message.answer(f"Уведомления включены. Письма будут отправляться на {recipient}.")
        else:
            await message.answer(
                f"Уведомления включены для адреса {recipient}, но отправка пока не настроена на сервере. "
                "Администратору нужно задать SMTP_HOST и SMTP_FROM."
            )
        return
    if action in {"batch_cancel", "batch_status"}:
        if not message.from_user or not _can_check(message):
            await message.answer("У вас нет разрешения управлять пакетной проверкой.")
            return
        job_id = payload.get("job_id") if isinstance(payload.get("job_id"), str) else ""
        job = _batch_job_for_message(message, job_id)
        if job is None:
            await message.answer("Активной пакетной проверки с таким ID в этом чате нет.")
            return
        if action == "batch_cancel":
            job.cancel()
            await message.answer("⏹ Запрошена отмена пакетной проверки.")
            return
        current = job.current_query.value if job.current_query else "—"
        await message.answer(
            f"Задание {job.job_id}: {job.state.value}; "
            f"обработано {job.index}/{len(job.queries)}; текущий запрос: {current}."
        )
        return
    if action not in {"check", "watch", "unwatch"} or not isinstance(payload.get("query"), str):
        await message.answer("Неизвестное действие Mini App.")
        return
    if payload["action"] in {"watch", "unwatch"}:
        if not _allowed(message) or not message.from_user or watch_store is None:
            await message.answer("У вас нет разрешения на мониторинг компаний.")
            return
        if payload["action"] == "watch":
            watch_store.add(message.chat.id, payload["query"], message.from_user.id)
            _audit(message, "watch_added", "company_added_from_mini_app")
            await message.answer("Компания добавлена в мониторинг.")
        else:
            removed = watch_store.remove(message.chat.id, payload["query"])
            if removed:
                _audit(message, "watch_removed", "company_removed_from_mini_app")
            await message.answer(
                "Компания удалена из мониторинга." if removed else "Компания не найдена в мониторинге."
            )
        return
    await _run_check(message, payload["query"])


@router.callback_query(F.data.startswith("shared_list:"))
async def shared_list_callback(callback: CallbackQuery) -> None:
    if not callback.message or not callback.from_user or shared_list_store is None:
        await callback.answer("Не удалось определить чат или список.", show_alert=True)
        return
    share_id = (callback.data or "").split(":", 1)[1]
    shared = shared_list_store.get(share_id, callback.message.chat.id)
    if shared is None:
        await callback.answer("Список устарел или относится к другому чату.", show_alert=True)
        return
    if not _mini_app_api_url():
        await callback.answer("Для защищённой передачи списка требуется настроенный Mini App API.", show_alert=True)
        return
    if str(callback.from_user.id) == shared.owner_id:
        await callback.answer("Этот список уже принадлежит вам.", show_alert=True)
        return
    if not _can_check_user(callback.message.chat.id, callback.from_user):
        await callback.answer("У вас нет разрешения на проверки в этом чате.", show_alert=True)
        return
    shared_url = _mini_app_link(
        chat_id=callback.message.chat.id,
        chat_type=callback.message.chat.type,
        shared_list_id=shared.share_id,
    )
    if len(shared_url) > MAX_SHARED_MINI_APP_URL_LENGTH:
        await callback.answer(
            "Список слишком большой для передачи одной кнопкой. Удалите лишние компании и повторите попытку.",
            show_alert=True,
        )
        return
    keyboard = _mini_app_keyboard(
        chat_id=callback.message.chat.id,
        chat_type=callback.message.chat.type,
        shared_list_id=shared.share_id,
    )
    if keyboard is None:
        await callback.answer("Mini App пока не опубликован.", show_alert=True)
        return
    await callback.answer("Список подготовлен для добавления в ваш MiniApp.")
    await callback.message.answer(
        f"Нажмите кнопку, чтобы добавить себе {len(shared.queries)} компаний.",
        reply_markup=keyboard,
    )


@router.message(Command("learning_queue"))
async def learning_queue(message: Message) -> None:
    if not message.from_user or not _is_root(message) or learning_store is None:
        await message.answer("Команда доступна администраторам.")
        return
    events = learning_store.pending(message.chat.id)
    if not events:
        await message.answer("Очередь обучения пуста.")
        return
    lines = ["Очередь обратной связи:"]
    for event in events:
        feedback = event.feedback.value if event.feedback else "без оценки"
        lines.append(f"{event.event_id} — {event.query_kind}, {event.response_state}, {feedback}")
    await message.answer("\n".join(lines))


@router.message(Command("learning_review"))
async def learning_review(message: Message, command: CommandObject) -> None:
    if not message.from_user or not _is_root(message) or learning_store is None:
        await message.answer("Команда доступна администраторам.")
        return
    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 2 or parts[1] not in {"approved", "rejected"}:
        await message.answer("Формат: /learning_review ID approved|rejected [исправленный ответ]")
        return
    saved = learning_store.review(
        event_id=parts[0],
        chat_id=message.chat.id,
        reviewer_id=str(message.from_user.id),
        approved=parts[1] == "approved",
        correction=parts[2] if len(parts) == 3 else None,
    )
    _audit(message, "learning_review", f"approved={parts[1] == 'approved'}")
    await message.answer("Запись обработана." if saved else "Запись не найдена.")


@router.message(Command("learning_export"))
async def learning_export(message: Message, command: CommandObject) -> None:
    if not message.from_user or not _is_root(message) or learning_store is None:
        await message.answer("Команда доступна администраторам.")
        return
    export_path = os.getenv("LEARNING_EXPORT_PATH", "data/learning.jsonl")
    count = learning_store.export_jsonl(export_path, chat_id=message.chat.id)
    _audit(message, "learning_export", f"count={count}")
    await message.answer(f"Экспортировано одобренных примеров: {count}.")


def _target_user(message: Message, command: CommandObject) -> tuple[str | None, str | None]:
    """Возвращает username и user_id из аргумента или сообщения-ответа."""

    target = (command.args or "").split(maxsplit=1)[0].strip() if command.args else ""
    replied = message.reply_to_message.from_user if message.reply_to_message else None
    if not target and replied:
        target = replied.username or ""
    if not target:
        return None, None
    tag = normalize_tag(target)
    target_id = str(replied.id) if replied and normalize_tag(replied.username or "") == tag else None
    return tag, target_id


def _requested_role(command: CommandObject) -> str:
    parts = (command.args or "").split()
    role = parts[1].casefold() if len(parts) > 1 else "checker"
    return role if role in {"viewer", "checker", "reviewer", "manager"} else "checker"


@router.message(Command("grant"))
async def grant_access(message: Message, command: CommandObject) -> None:
    if not _is_root(message) or not message.from_user or access_store is None:
        await message.answer("Команда доступна Главному администратору.")
        return
    tag, target_id = _target_user(message, command)
    if not tag:
        await message.answer("Формат: /grant @username или ответьте этой командой на сообщение пользователя.")
        return
    if access_store.grant(
        message.chat.id,
        username=tag,
        user_id=target_id,
        granted_by=message.from_user.id,
        role=_requested_role(command),
    ):
        _audit(message, "access_granted", f"role={_requested_role(command)}")
        await message.answer(f"Пользователю @{tag} выдан доступ в этом чате с ролью {_requested_role(command)}.")
    else:
        await message.answer(
            "Не удалось выдать доступ. Для уже привязанного пользователя выполните /grant ответом на его сообщение."
        )


@router.message(Command("revoke"))
async def revoke_access(message: Message, command: CommandObject) -> None:
    if not _is_root(message) or not message.from_user or access_store is None:
        await message.answer("Команда доступна Главному администратору.")
        return
    tag, target_id = _target_user(message, command)
    if not tag:
        await message.answer("Формат: /revoke @username или ответьте этой командой на сообщение пользователя.")
        return
    if access_store.revoke(
        message.chat.id,
        username=tag,
        user_id=target_id,
        revoked_by=message.from_user.id,
    ):
        _audit(message, "access_revoked", "user_access_revoked")
        await message.answer(f"Доступ пользователя @{tag} отозван в этом чате.")
    else:
        await message.answer("Нельзя отозвать доступ Главного администратора или указан некорректный тег.")


@router.message(Command("trusted"))
async def trusted_users(message: Message) -> None:
    if not _is_root(message) or access_store is None:
        await message.answer("Команда доступна Главному администратору.")
        return
    users = access_store.list_trusted(message.chat.id)
    if not users:
        await message.answer("В этом чате нет выданных прав. Главный администратор назначен в конфигурации бота.")
        return
    lines = ["Доверенные пользователи этого чата:", "Главный администратор — системная роль"]
    lines.extend(f"@{user.tag} — {user.role}" for user in users)
    await message.answer("\n".join(lines))


async def _handle_menu_action(message: Message, text: str) -> bool:
    """Обрабатывает подписи постоянной клавиатуры и сообщает, что действие принято."""

    # Telegram-клиенты могут добавлять variation selector к emoji в подписи
    # кнопки. Убираем его, чтобы нажатие не зависело от конкретного клиента.
    action = text.strip().replace("\ufe0e", "").replace("\ufe0f", "")
    if action == "🔎 Проверить компанию":
        await _open_check_mini_app(message)
    elif action == "📋 История проверок":
        await _show_history(message)
    elif action == "📊 Проверить все":
        await check_all(message)
    elif action == "📁 Загрузить CSV/XLSX":
        await message.answer(
            "Прикрепите файл CSV или XLSX и добавьте к подписи сообщения «/bulk».\n"
            "Файл будет обработан последовательно, а итог будет отправлен в Excel."
        )
    elif action == "👁 Мониторинг":
        await watched_command(message)
    elif action == "📄 Лицензии":
        await license_list_command(message)
    elif action == "🆘 Помощь":
        await help_command(message)
    elif action == "🧠 Навыки":
        await skills_command(message)
    else:
        return False
    return True


@router.message(F.text.in_(BOT_MENU_ACTIONS))
async def menu_action(message: Message) -> None:
    """Обрабатывает нажатия постоянной клавиатуры отдельным маршрутом."""

    if message.text:
        logging.info("Обработка действия меню Telegram: %s", message.text)
        await _handle_menu_action(message, message.text)


@router.message()
async def natural_language(message: Message) -> None:
    """Обрабатывает русские сообщения, начинающиеся с обращения «Налог»."""

    if not message.text:
        return
    if await _handle_menu_action(message, message.text):
        return
    request = parse_natural_request(message.text)
    if request is None:
        return
    if request.intent is NaturalIntent.HELP:
        await message.answer(
            "Я — Налог, агент проверки юридических лиц. "
            "Обратитесь ко мне, например: «Налог, проверь компанию с ИНН 7707083893».\n\n" + skills_text()
        )
    elif request.intent is NaturalIntent.GREETING:
        await message.answer(
            "Здравствуйте. Я готов проверить юридическое лицо по ИНН, ОГРН, КПП, "
            "названию, адресу или другим данным внутри MiniApp. Напишите: «Налог, проверка»."
        )
    elif request.intent is NaturalIntent.OPEN_CHECK_APP:
        await _open_check_mini_app(message)
    elif request.intent is NaturalIntent.CHECK:
        await _run_check(message, request.query_text)
    elif request.intent is NaturalIntent.LICENSE:
        # Используем тот же парсер реквизитов, что и обычная проверка; команда
        # /license далее гарантирует, что отсутствие записи не трактуется как
        # отсутствие лицензии.
        await license_command(message, CommandObject(args=request.query_text))
    elif request.intent is NaturalIntent.CHECK_ALL:
        await check_all(message)
    elif request.intent is NaturalIntent.HISTORY:
        await _show_history(message)
    else:
        if not _allowed(message) or not message.from_user:
            await message.answer("У вас нет разрешения на разговорный режим.")
            return
        if chat_skill is None:
            await message.answer(
                "Разговорный режим пока не настроен. Проверка доступна через MiniApp: напишите «Налог, проверка»."
            )
            return
        try:
            answer = await chat_skill.reply(
                f"{message.chat.id}:{message.from_user.id}",
                message.text,
            )
        except ChatSkillError:
            logging.exception("Ошибка разговорного skill")
            await message.answer("Не удалось получить ответ разговорного модуля. Попробуйте ещё раз.")
        else:
            await message.answer(answer)


async def _monitor_loop(bot: Bot, interval_seconds: int) -> None:
    """Периодически проверяет watchlist и сообщает лишь об изменениях.

    Ошибка источника пропускает цикл и не создаёт ложное уведомление об
    отсутствии риска. Первая успешная проверка формирует базовый снимок.
    """
    notified_licenses: set[tuple[str, str, str, str]] = set()
    semaphore = asyncio.Semaphore(_positive_env_int("MONITOR_CONCURRENCY", 2))
    while True:
        try:
            if watch_store and agent:
                for chat_id, raw_query, _ in watch_store.entries():
                    async with semaphore:
                        result = None
                        for attempt in range(3):
                            try:
                                query = parse_search_query(raw_query)
                                result = await agent.check(query)
                                break
                            except Exception:
                                if attempt == 2:
                                    logging.warning("Мониторинг пропущен для %s", raw_query, exc_info=True)
                                else:
                                    await asyncio.sleep(2**attempt)
                        if result is None:
                            continue
                    record = result.record
                    snapshot = "|".join(
                        str(item)
                        for item in (
                            record.name,
                            record.inn,
                            record.ogrn,
                            record.kpp,
                            record.status,
                            record.inaccuracy_state.value,
                            ",".join(record.inaccuracy_markers),
                        )
                    )
                    previous_digest, previous_state = watch_store.snapshot_state(chat_id, raw_query)
                    current_state = record.inaccuracy_state.value
                    if watch_store.update_snapshot_with_state(
                        chat_id,
                        raw_query,
                        snapshot,
                        inaccuracy_state=current_state,
                    ):
                        await bot.send_message(
                            int(chat_id),
                            "⚠️ Изменились данные компании в мониторинге.\n\n" + format_assessment(result),
                        )
                    if previous_digest is None:
                        if notification_store:
                            notification_store.mark_alert_state(chat_id, raw_query, current_state)
                    elif current_state != "present" and notification_store:
                        notification_store.mark_alert_state(chat_id, raw_query, current_state)
                    elif current_state == "present" and notification_store:
                        # Existing rows created before state tracking are treated
                        # as a baseline on their first post-upgrade check.
                        if previous_state is None:
                            notification_store.mark_alert_state(chat_id, raw_query, "present")
                            continue
                        recipient = notification_store.get(chat_id) if notification_store else None
                        alert_state = notification_store.alert_state(chat_id, raw_query)
                        if recipient and alert_state != "present":
                            if not smtp_config.configured:
                                logging.warning(
                                    "Email-уведомление не отправлено для %s: SMTP не настроен",
                                    raw_query,
                                )
                            else:
                                title = record.name or record.inn or raw_query
                                subject = f"[Налог] Недостоверные сведения: {title}"
                                body = (
                                    "В результате очередной проверки ФНС обнаружена отметка о "
                                    "недостоверности сведений.\n\n"
                                    f"Запрос: {raw_query}\n"
                                    f"Компания: {title}\n"
                                    f"ИНН: {record.inn or 'не указан'}\n"
                                    f"ОГРН: {record.ogrn or 'не указан'}\n"
                                    f"КПП: {record.kpp or 'не указан'}\n"
                                    f"Маркеры: {', '.join(record.inaccuracy_markers) or 'не указаны'}\n"
                                    f"Источник: {record.source_url}\n"
                                    f"Проверено: {record.fetched_at:%d.%m.%Y %H:%M UTC}\n\n"
                                    f"{format_assessment(result)}"
                                )
                                try:
                                    await asyncio.to_thread(send_email, smtp_config, recipient, subject, body)
                                except Exception:
                                    logging.exception("Не удалось отправить email-уведомление для %s", raw_query)
                                else:
                                    notification_store.mark_alert_state(chat_id, raw_query, "present")
            if license_store:
                for license_record in license_store.expiring(_positive_env_int("LICENSE_ALERT_DAYS", 30)):
                    days = license_record.days_left()
                    # Лицензионные уведомления направляются в чаты, где есть
                    # компании из watchlist и совпадает ИНН.
                    if watch_store:
                        for chat_id, raw_query, _ in watch_store.entries():
                            if license_record.inn not in raw_query:
                                continue
                            key = (str(chat_id), license_record.license_id, str(license_record.expires_at), str(days))
                            if key in notified_licenses:
                                continue
                            notified_licenses.add(key)
                            await bot.send_message(
                                int(chat_id),
                                f"⏰ Лицензия {license_record.license_id} истекает через {days} дней.\n"
                                f"{_license_line(license_record)}",
                            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка фонового мониторинга")
        await asyncio.sleep(interval_seconds)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().casefold() in {"1", "true", "yes", "да"}


def _smtp_config_from_env() -> SmtpConfig:
    return SmtpConfig(
        host=os.getenv("SMTP_HOST", "").strip(),
        port=_positive_env_int("SMTP_PORT", 587),
        username=os.getenv("SMTP_USER", "").strip(),
        password=os.getenv("SMTP_PASSWORD", ""),
        sender=os.getenv("SMTP_FROM", "").strip() or os.getenv("SMTP_USER", "").strip(),
        starttls=_env_bool("SMTP_STARTTLS", True),
        timeout=_positive_env_float("SMTP_TIMEOUT_SECONDS", 20.0),
    )


def _telegram_proxy() -> str | None:
    """Возвращает проверенный прокси для Telegram API без вывода секрета в логи."""

    value = os.getenv("TELEGRAM_PROXY", "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme.casefold() not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise RuntimeError(
            "TELEGRAM_PROXY должен быть URL HTTPS/HTTP или SOCKS5-прокси, например socks5://user:password@host:1080."
        )
    return value


async def _run() -> None:
    global \
        agent, \
        access_store, \
        learning_store, \
        history_store, \
        license_store, \
        audit_store, \
        watch_store, \
        shared_list_store
    global notification_store, smtp_config, chat_skill
    global reaction_settings
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN в .env")
    if configured_main_admin_user_id() is None:
        raise RuntimeError(
            "Не задан MAIN_ADMIN_USER_ID: укажите числовой Telegram user_id главного администратора."
        )
    hash_salt = os.getenv("LEARNING_HASH_SALT", "").strip()
    if not hash_salt:
        raise RuntimeError("Не задан LEARNING_HASH_SALT: укажите стабильную случайную соль для защиты идентификаторов.")
    access_store = ChatAccessStore(
        os.getenv("ACCESS_DB_PATH", "data/access.sqlite3"),
    )
    primary_fns_client = FnsEgrulClient(
        base_url=os.getenv("FNS_BASE_URL", "").strip() or "https://egrul.nalog.ru/",
        timeout=_positive_env_float("FNS_TIMEOUT_SECONDS", 20.0),
    )
    fns_client = primary_fns_client
    local_db_path = os.getenv("FNS_LOCAL_DB_PATH", "").strip()
    if _env_bool("FNS_LOCAL_DB_ENABLED") and local_db_path:
        fns_client = FnsFallbackClient(primary_fns_client, LocalEgrulClient(local_db_path))
        logging.info("Локальный слепок ФНС включён как резервный источник: %s", local_db_path)
    deep_checker = None
    if _env_bool("MCP_FNS_CHECK_ENABLED"):
        deep_checker = AtomnoFnsCheckAdapter(
            include_extended_risks=_env_bool("MCP_FNS_CHECK_EXTENDED_RISKS", True),
            lawsuits_threshold_rub=_positive_env_float("MCP_FNS_CHECK_LAWSUITS_THRESHOLD_RUB", 1_000_000.0),
        )
    agent = LegalEntityAgent(client=fns_client, deep_checker=deep_checker)
    learning_store = LearningStore(
        os.getenv("LEARNING_DB_PATH", "data/learning.sqlite3"),
        store_raw=_env_bool("LEARNING_STORE_RAW_REQUESTS"),
        hash_salt=hash_salt,
    )
    history_store = HistoryStore(
        os.getenv("HISTORY_DB_PATH", "data/history.sqlite3"),
        hash_salt=hash_salt,
    )
    license_store = LicenseStore(os.getenv("LICENSE_DB_PATH", "data/licenses.sqlite3"))
    audit_store = AuditStore(os.getenv("AUDIT_DB_PATH", "data/audit.sqlite3"))
    watch_store = WatchStore(os.getenv("WATCH_DB_PATH", "data/watchlist.sqlite3"))
    shared_list_store = SharedListStore(os.getenv("SHARED_LIST_DB_PATH", "data/shared_lists.sqlite3"))
    notification_store = EmailSubscriptionStore(os.getenv("NOTIFICATION_DB_PATH", "data/notifications.sqlite3"))
    smtp_config = _smtp_config_from_env()
    chat_skill = ChatSkill.from_env()
    reaction_settings = ReactionSettings.from_env()
    retention = os.getenv("LEARNING_RETENTION_DAYS", "").strip()
    if retention:
        try:
            learning_store.purge_older_than(int(retention))
        except ValueError as exc:
            raise RuntimeError("LEARNING_RETENTION_DAYS должен быть целым числом дней.") from exc
    proxy = _telegram_proxy()
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(token=token, session=session) if session else Bot(token=token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    mini_app_api_runner = await _start_mini_app_api(bot, token)
    monitor_task = asyncio.create_task(_monitor_loop(bot, _positive_env_int("MONITOR_INTERVAL_SECONDS", 86400)))
    try:
        await dispatcher.start_polling(bot)
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        if mini_app_api_runner:
            await mini_app_api_runner.cleanup()
        await bot.session.close()
        if agent:
            await agent.close()
        if learning_store:
            learning_store.close()
        if access_store:
            access_store.close()
        if history_store:
            history_store.close()
        if license_store:
            license_store.close()
        if audit_store:
            audit_store.close()
        if watch_store:
            watch_store.close()
        if shared_list_store:
            shared_list_store.close()
        if notification_store:
            notification_store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())
