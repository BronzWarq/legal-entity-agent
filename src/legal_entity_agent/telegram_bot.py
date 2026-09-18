from __future__ import annotations

import asyncio
import logging
import os
import secrets

from aiogram import Bot, Dispatcher, Router
from aiogram import F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from .agent import LegalEntityAgent
from .chat_skill import ChatSkill, ChatSkillError
from .conversation import NaturalIntent, parse_natural_request
from .fns_client import FnsError
from .history import HistoryEntry, HistoryStore
from .identifiers import InvalidIdentifier, parse_search_query
from .learning import FeedbackLabel, LearningStore
from .permissions import ChatAccessStore, is_main_admin, normalize_tag
from .render import format_assessment

router = Router()
agent: LegalEntityAgent | None = None
access_store: ChatAccessStore | None = None
learning_store: LearningStore | None = None
history_store: HistoryStore | None = None
chat_skill: ChatSkill | None = None


def _allowed(message: Message) -> bool:
    return bool(
        access_store
        and message.from_user
        and access_store.is_allowed(
            message.chat.id,
            user_id=message.from_user.id,
            username=message.from_user.username,
        )
    )


def _is_root(message: Message) -> bool:
    return bool(message.from_user and is_main_admin(message.from_user.username))


def help_text() -> str:
    """Возвращает единый список команд для справки и документации бота."""

    return (
        "Команды бота:\n\n"
        "/start — запустить бота и получить краткую инструкцию.\n"
        "/help — показать этот список команд.\n"
        "/check реквизиты — проверить юридическое лицо по данным ФНС.\n"
        "  Можно указать ИНН, ОГРН, КПП, название, адрес или несколько реквизитов.\n"
        "  Можно написать свободно: «Налог, проверь компанию с ИНН 7707083893».\n"
        "/history — показать ранее выполненные проверки в текущем чате.\n"
        "/history ID — показать сохранённый итог конкретной проверки.\n"
        "/check_all (или /check all) — повторно проверить все уникальные юрлица из базы истории (администратор).\n"
        "/feedback ID ОЦЕНКА — отправить оценку результата проверки.\n"
        "  Оценки: correct, incorrect или needs_review.\n\n"
        "/chat_reset — очистить память разговорного диалога в текущем чате.\n\n"
        "Команды Главного администратора @Sholomon:\n"
        "/grant @username — выдать пользователю доступ в текущем чате.\n"
        "/revoke @username — отозвать доступ пользователя в текущем чате.\n"
        "/trusted — показать доверенных пользователей текущего чата.\n"
        "/learning_queue — показать очередь обратной связи.\n"
        "/learning_review ID approved|rejected — проверить пример обучения.\n"
        "/learning_export — экспортировать одобренные примеры обучения."
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Агент готов. Для проверки используйте /check и реквизиты юридического лица.\n"
        "Можно обратиться свободной фразой: «Налог, проверь компанию с ИНН 7707083893».\n"
        "После ответа можно поставить оценку кнопками обратной связи.\n"
        "Для полного списка команд используйте /help."
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(help_text())


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
    await _run_check(message, command.args or "")


async def _run_check(message: Message, raw_query: str) -> None:
    if not _allowed(message):
        await message.answer("У вас нет разрешения на выполнение этой команды.")
        return
    if not raw_query.strip():
        await message.answer("Формат: /check ИНН 7707083893 КПП 770401001")
        return
    if agent is None:
        await message.answer("Проверка сейчас недоступна: агент не настроен.")
        return
    try:
        query = parse_search_query(raw_query)
        result = await agent.check(query)
    except InvalidIdentifier as exc:
        await message.answer(f"Не удалось распознать реквизиты: {exc}")
    except FnsError as exc:
        if learning_store and message.from_user:
            learning_store.record_failure(
                actor_id=str(message.from_user.id), identifier=query, error=str(exc)
            )
        await message.answer(f"Проверка ФНС не выполнена: {exc}")
    else:
        report = format_assessment(result)
        event_id = None
        if learning_store and message.from_user:
            event_id = learning_store.record_check(
                actor_id=str(message.from_user.id),
                identifier=query,
                assessment=result,
                rendered_response=report,
            )
        if history_store and message.from_user and event_id:
            history_store.record_check(
                event_id=event_id,
                chat_id=message.chat.id,
                actor_id=message.from_user.id,
                query=query,
                assessment=result,
                report=report,
            )
        suffix = "\n\nОцените результат, чтобы агент мог улучшаться после проверки администратора."
        await message.answer(
            report + suffix,
            reply_markup=_feedback_keyboard(event_id) if event_id else None,
        )


@router.callback_query(F.data.startswith("feedback:"))
async def feedback_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message or not _allowed(callback.message):
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
        actor_id=str(callback.from_user.id),
        label=label,
        is_admin=is_main_admin(callback.from_user.username),
    )
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
        actor_id=str(message.from_user.id),
        label=label,
        note=parts[2] if len(parts) == 3 else None,
        is_admin=is_main_admin(message.from_user.username),
    )
    await message.answer("Обратная связь сохранена." if saved else "Запись не найдена или недоступна.")


def _history_line(entry: HistoryEntry) -> str:
    title = entry.name or entry.query_text
    title = title if len(title) <= 70 else title[:67] + "..."
    identifiers = " / ".join(
        value for value in (entry.inn, entry.kpp, entry.ogrn) if value
    )
    state = {
        "present": "есть отметка о недостоверности",
        "absent": "отметка о недостоверности отсутствует",
        "not_reported": "не распознано",
    }.get(entry.inaccuracy_state, entry.inaccuracy_state)
    details = f"; {identifiers}" if identifiers else ""
    return f"{entry.event_id} — {entry.created_at:%d.%m.%Y %H:%M}; {title}{details}; {state}"


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


@router.message(Command("check_all", "checkall"))
async def check_all(message: Message) -> None:
    """Повторно проверяет все уникальные организации, сохранённые в истории."""

    if not _is_root(message):
        await message.answer("Команда доступна Главному администратору @Sholomon.")
        return
    if not message.from_user or agent is None or history_store is None:
        await message.answer("Массовая проверка сейчас недоступна: хранилище не настроено.")
        return

    queries = history_store.list_unique_queries()
    if not queries:
        await message.answer("В базе истории нет сохранённых юридических лиц для проверки.")
        return

    concurrency = _positive_env_int("CHECK_ALL_CONCURRENCY", 3)
    await message.answer(
        f"Запускаю повторную проверку {len(queries)} уникальных юридических лиц. "
        f"Одновременно выполняется до {concurrency} запросов."
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def process(query):
        async with semaphore:
            try:
                result = await agent.check(query)
            except FnsError as exc:
                if learning_store:
                    learning_store.record_failure(
                        actor_id=str(message.from_user.id), identifier=query, error=str(exc)
                    )
                return False, f"❌ {query.value} — {exc}"
            except Exception as exc:  # pragma: no cover - защитный контур для массовой операции
                logging.exception("Ошибка массовой проверки запроса %s", query.value)
                if learning_store:
                    learning_store.record_failure(
                        actor_id=str(message.from_user.id), identifier=query, error="internal error"
                    )
                return False, f"❌ {query.value} — внутренняя ошибка проверки"

            report = format_assessment(result)
            event_id = (
                learning_store.record_check(
                    actor_id=str(message.from_user.id),
                    identifier=query,
                    assessment=result,
                    rendered_response=report,
                )
                if learning_store
                else secrets.token_urlsafe(9)
            )
            history_store.record_check(
                event_id=event_id,
                chat_id=message.chat.id,
                actor_id=message.from_user.id,
                query=query,
                assessment=result,
                report=report,
            )
            return True, _bulk_result_line(query.value, event_id, result)

    outcomes = await asyncio.gather(*(process(query) for query in queries))
    succeeded = sum(1 for success, _ in outcomes if success)
    failed = len(outcomes) - succeeded
    lines = [
        f"Массовая проверка завершена: успешно — {succeeded}, с ошибкой — {failed}.",
        "Результаты сохранены в историю текущего чата:",
    ]
    lines.extend(line for _, line in outcomes)
    await _answer_chunks(message, lines)


@router.message(Command("learning_queue"))
async def learning_queue(message: Message) -> None:
    if not message.from_user or not _is_root(message) or learning_store is None:
        await message.answer("Команда доступна администраторам.")
        return
    events = learning_store.pending()
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
        reviewer_id=str(message.from_user.id),
        approved=parts[1] == "approved",
        correction=parts[2] if len(parts) == 3 else None,
    )
    await message.answer("Запись обработана." if saved else "Запись не найдена.")


@router.message(Command("learning_export"))
async def learning_export(message: Message, command: CommandObject) -> None:
    if not message.from_user or not _is_root(message) or learning_store is None:
        await message.answer("Команда доступна администраторам.")
        return
    export_path = os.getenv("LEARNING_EXPORT_PATH", "data/learning.jsonl")
    count = learning_store.export_jsonl(export_path)
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


@router.message(Command("grant"))
async def grant_access(message: Message, command: CommandObject) -> None:
    if not _is_root(message) or not message.from_user or access_store is None:
        await message.answer("Команда доступна Главному администратору @Sholomon.")
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
    ):
        await message.answer(f"Пользователю @{tag} выдан доступ в этом чате.")
    else:
        await message.answer("Нельзя выдать доступ Главному администратору или указан некорректный тег.")


@router.message(Command("revoke"))
async def revoke_access(message: Message, command: CommandObject) -> None:
    if not _is_root(message) or not message.from_user or access_store is None:
        await message.answer("Команда доступна Главному администратору @Sholomon.")
        return
    tag, _ = _target_user(message, command)
    if not tag:
        await message.answer("Формат: /revoke @username или ответьте этой командой на сообщение пользователя.")
        return
    if access_store.revoke(message.chat.id, username=tag, revoked_by=message.from_user.id):
        await message.answer(f"Доступ пользователя @{tag} отозван в этом чате.")
    else:
        await message.answer("Нельзя отозвать доступ Главного администратора или указан некорректный тег.")


@router.message(Command("trusted"))
async def trusted_users(message: Message) -> None:
    if not _is_root(message) or access_store is None:
        await message.answer("Команда доступна Главному администратору @Sholomon.")
        return
    users = access_store.list_trusted(message.chat.id)
    if not users:
        await message.answer("В этом чате нет выданных прав. Главный администратор: @Sholomon.")
        return
    lines = ["Доверенные пользователи этого чата:", "@Sholomon — Главный администратор"]
    lines.extend(f"@{user.tag}" for user in users)
    await message.answer("\n".join(lines))


@router.message()
async def natural_language(message: Message) -> None:
    """Обрабатывает русские сообщения, начинающиеся с обращения «Налог»."""

    if not message.text:
        return
    request = parse_natural_request(message.text)
    if request is None:
        return
    if request.intent is NaturalIntent.HELP:
        await message.answer(
            "Я — Налог, агент проверки юридических лиц. "
            "Обратитесь ко мне, например: «Налог, проверь компанию с ИНН 7707083893».\n\n"
            + help_text()
        )
    elif request.intent is NaturalIntent.GREETING:
        await message.answer(
            "Здравствуйте. Я готов проверить юридическое лицо по ИНН, ОГРН, КПП, "
            "названию, адресу или другим данным. Например: «Налог, проверь компанию с ИНН 7707083893»."
        )
    elif request.intent is NaturalIntent.CHECK:
        await _run_check(message, request.query_text)
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
                "Разговорный режим пока не настроен. "
                "Проверки доступны через /check или фразу «Налог, проверь компанию с ИНН …»."
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


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().casefold() in {"1", "true", "yes", "да"}


async def _run() -> None:
    global agent, access_store, learning_store, history_store, chat_skill
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN в .env")
    access_store = ChatAccessStore(
        os.getenv("ACCESS_DB_PATH", "data/access.sqlite3"),
    )
    agent = LegalEntityAgent()
    learning_store = LearningStore(
        os.getenv("LEARNING_DB_PATH", "data/learning.sqlite3"),
        store_raw=_env_bool("LEARNING_STORE_RAW_REQUESTS"),
        hash_salt=os.getenv("LEARNING_HASH_SALT", ""),
    )
    history_store = HistoryStore(
        os.getenv("HISTORY_DB_PATH", "data/history.sqlite3"),
        hash_salt=os.getenv("LEARNING_HASH_SALT", ""),
    )
    chat_skill = ChatSkill.from_env()
    retention = os.getenv("LEARNING_RETENTION_DAYS", "").strip()
    if retention:
        try:
            learning_store.purge_older_than(int(retention))
        except ValueError as exc:
            raise RuntimeError("LEARNING_RETENTION_DAYS должен быть целым числом дней.") from exc
    bot = Bot(token=token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        if learning_store:
            learning_store.close()
        if access_store:
            access_store.close()
        if history_store:
            history_store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())
