from __future__ import annotations

import asyncio
import logging
import os
import secrets
from datetime import UTC, datetime
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    WebAppInfo,
)
from dotenv import load_dotenv

from .agent import LegalEntityAgent
from .audit import AuditStore
from .batch_jobs import BatchJob, BatchJobRegistry, BatchJobState
from .batch_review import BatchReviewStore
from .bulk_import import parse_upload
from .chat_skill import ChatSkill, ChatSkillError
from .conversation import NaturalIntent, parse_natural_request
from .deep_check import AtomnoFnsCheckAdapter
from .excel_export import build_check_workbook, row_from_assessment, row_from_error
from .fns_client import FnsBlockedError, FnsEgrulClient, FnsError, FnsNotFoundError, FnsTransientError
from .history import HistoryEntry, HistoryStore
from .identifiers import InvalidIdentifier, parse_search_query
from .learning import FeedbackLabel, LearningStore
from .licenses import LicenseStore, new_license
from .models import SearchQuery
from .permissions import ChatAccessStore, is_main_admin, normalize_tag
from .reactions import ReactionSettings
from .render import format_assessment
from .watchlist import WatchStore

router = Router()
agent: LegalEntityAgent | None = None
access_store: ChatAccessStore | None = None
learning_store: LearningStore | None = None
history_store: HistoryStore | None = None
license_store: LicenseStore | None = None
audit_store: AuditStore | None = None
watch_store: WatchStore | None = None
batch_review_store: BatchReviewStore | None = None
chat_skill: ChatSkill | None = None
reaction_settings = ReactionSettings()
batch_jobs = BatchJobRegistry()
batch_tasks: dict[str, asyncio.Task[None]] = {}


def _audit(message: Message, action: str, details: str = "") -> None:
    if audit_store and message.from_user:
        try:
            audit_store.record(chat_id=message.chat.id, actor_id=message.from_user.id,
                               action=action, details=details)
        except Exception:
            logging.exception("Не удалось записать действие в аудит")


def _mini_app_url() -> str:
    """Возвращает безопасный адрес Mini App, опубликованный по HTTPS."""
    url = os.getenv("MINI_APP_URL", "").strip()
    return url if url.startswith("https://") else ""


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
    return bool(message.from_user and is_main_admin(message.from_user.username))


async def _react_to_message(message: Message, bot: Bot) -> None:
    """Ставит настроенную реакцию на сообщение доверенного пользователя.

    Неудача с реакцией не должна блокировать обработку команды или диалога:
    например, в чате может быть запрещено право бота реагировать на сообщения.
    """

    if (
        not reaction_settings.enabled
        or not message.from_user
        or message.from_user.is_bot
        or not _allowed(message)
    ):
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
        "/skills — рассказать, что умеет агент.\n"
        "/check реквизиты — проверить юридическое лицо по данным ФНС.\n"
        "  Можно указать ИНН, ОГРН, КПП, название, адрес или несколько реквизитов.\n"
        "  После проверки бот отправляет текст и Excel-файл с четырьмя столбцами.\n"
        "  Можно написать свободно: «Налог, проверь компанию с ИНН 7707083893».\n"
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
        "/app — открыть Telegram Mini App (если задан MINI_APP_URL).\n"
        "/check_all (или /check all) — повторно проверить все уникальные юрлица из базы истории (администратор).\n"
        "/batch_status — показать состояние текущей массовой проверки.\n"
        "/captcha_done ID — продолжить очередь после ручной CAPTCHA.\n"
        "/batch_cancel ID — отменить ожидающую массовую проверку.\n"
        "/batch_pending — список компаний, которые требуют ручной проверки или повтора.\n"
        "/feedback ID ОЦЕНКА — отправить оценку результата проверки.\n"
        "  Оценки: correct, incorrect или needs_review.\n\n"
        "/chat_reset — очистить память разговорного диалога в текущем чате.\n\n"
        "Реакции бота на сообщения доверенных пользователей настраиваются через REACTIONS_ENABLED, REACTION_EMOJIS и REACTION_MODE.\n\n"
        "Команды Главного администратора @Sholomon:\n"
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
        "📁 Массовые проверки — обработка CSV/XLSX и Excel-отчёт по результатам.\n"
        "🧾 Объяснение результата — команда /why показывает источник, время, маркеры и основания вывода.\n"
        "🗂 История — сохранение результатов и повторный просмотр предыдущих проверок.\n"
        "💬 Русский диалог — обработка обращений, начинающихся с «Налог».\n"
        "🧠 Контролируемое обучение — обратная связь пользователей с проверкой администратором.\n"
        "👥 Доступы — проверка доверенных пользователей, роли и отдельные права в каждом чате.\n"
        "📋 Аудит — журнал административных действий и операций.\n"
        "🖥 Mini App — таблица компаний, запуск проверки и добавление в мониторинг.\n\n"
        "Для команд используйте /help. Пример запроса:\n"
        "«Налог, проверь компанию с ИНН 7707083893»."
    )


@router.message(CommandStart())
async def start(message: Message, bot: Bot) -> None:
    url = _mini_app_url()
    if url:
        try:
            await bot.set_chat_menu_button(
                chat_id=message.chat.id,
                menu_button=MenuButtonWebApp(text="Налог", web_app=WebAppInfo(url=url)),
            )
        except Exception:
            logging.warning("Не удалось установить кнопку Mini App для чата %s", message.chat.id, exc_info=True)
    await message.answer(
        "Агент готов. Для проверки используйте /check и реквизиты юридического лица.\n"
        "Можно обратиться свободной фразой: «Налог, проверь компанию с ИНН 7707083893».\n"
        "После ответа можно поставить оценку кнопками обратной связи.\n"
        "Для полного списка команд используйте /help."
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(help_text())


@router.message(Command("skills", "about"))
async def skills_command(message: Message) -> None:
    await message.answer(skills_text())


@router.message(Command("app"))
async def app_command(message: Message, bot: Bot) -> None:
    url = _mini_app_url()
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
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть Mini App", web_app=WebAppInfo(url=url)),
        ]]),
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
    await _run_check(message, command.args or "")


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
                    actor_id=str(message.from_user.id), identifier=query, error=str(exc)
                )
            except Exception:
                logging.exception("Не удалось сохранить ошибку проверки в журнал обучения")
        await message.answer(f"Проверка ФНС не выполнена: {exc}")
    except Exception:
        logging.exception("Непредвиденная ошибка одиночной проверки")
        if learning_store and message.from_user and query is not None:
            try:
                learning_store.record_failure(
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
        actor_id=str(callback.from_user.id),
        label=label,
        is_admin=is_main_admin(callback.from_user.username),
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
        actor_id=str(message.from_user.id),
        label=label,
        note=parts[2] if len(parts) == 3 else None,
        is_admin=is_main_admin(message.from_user.username),
    )
    _audit(message, "feedback_added", f"label={label.value}")
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
    entry = history_store.get_for(event_id=event_id, chat_id=message.chat.id,
                                 actor_id=message.from_user.id, is_root=_is_root(message))
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
        await message.answer("Команда доступна Главному администратору @Sholomon.")
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
    record = new_license(license_id=parts[0], inn=parts[1], expires_at=expires,
                         notes=parts[3] if len(parts) == 4 else None)
    license_store.upsert(record)
    _audit(message, "license_add", "license_record_created")
    await message.answer(f"Лицензия {parts[0]} сохранена до {expires:%d.%m.%Y}.")


@router.message(Command("license_list"))
async def license_list_command(message: Message) -> None:
    if not _allowed(message) or license_store is None:
        await message.answer("У вас нет разрешения на просмотр лицензий.")
        return
    records = license_store.all()
    await message.answer("Лицензий в реестре нет." if not records else "\n".join(_license_line(item) for item in records))


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
        await message.answer("Команда доступна Главному администратору @Sholomon.")
        return
    rows = audit_store.recent(message.chat.id)
    if not rows:
        await message.answer("Журнал действий пуст.")
        return
    await message.answer("Журнал действий:\n" + "\n".join(
        f"{row['created_at'][:19]} — {row['action']} — {row['details']}" for row in rows
    ))


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
    await message.answer("Компания добавлена в список мониторинга. Периодический планировщик подключим после настройки интервала и канала уведомлений.")


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
    await message.answer("Компания удалена из списка мониторинга." if removed
                         else "Такой компании нет в списке мониторинга.")


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


def _captcha_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ CAPTCHA пройдена — продолжить",
                    callback_data=f"batch_captcha_resume:{job_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⏹ Отменить пакет",
                    callback_data=f"batch_captcha_cancel:{job_id}",
                )
            ],
        ]
    )


def _batch_captcha_url() -> str:
    value = os.getenv("FNS_PUBLIC_CHECK_URL", "https://pb.nalog.ru/").strip()
    return value if value.startswith("https://") else "https://pb.nalog.ru/"


async def _record_batch_failure(job: BatchJob, query: SearchQuery, error: str) -> None:
    if learning_store is None:
        return
    try:
        learning_store.record_failure(
            actor_id=str(job.actor_id),
            identifier=query,
            error=error,
        )
    except Exception:
        logging.exception("Не удалось сохранить ошибку пакетной проверки в журнал обучения")


def _batch_review_id(job: BatchJob) -> str:
    return f"{job.job_id}:{job.index}"


def _record_batch_pending(job: BatchJob, query: SearchQuery, reason: str) -> str:
    review_id = _batch_review_id(job)
    if batch_review_store:
        try:
            batch_review_store.record_pending(
                review_id=review_id,
                chat_id=job.chat_id,
                actor_id=job.actor_id,
                query_text=query.value,
                reason=reason,
                source=job.source,
            )
        except Exception:
            logging.exception("Не удалось сохранить компанию для ручной проверки")
    return review_id


def _resolve_batch_review(job: BatchJob, review_id: str | None = None) -> None:
    if batch_review_store:
        try:
            batch_review_store.resolve(review_id or _batch_review_id(job))
        except Exception:
            logging.exception("Не удалось закрыть запись ручной проверки")


async def _record_batch_success(message: Message, job: BatchJob, query: SearchQuery, result) -> tuple[str, str]:
    report = format_assessment(result)
    event_id = secrets.token_urlsafe(9)
    if learning_store:
        try:
            event_id = learning_store.record_check(
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
    """Последовательно выполнить пакет и ждать пользователя на CAPTCHA."""

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
                except FnsBlockedError:
                    review_id = _record_batch_pending(job, query, "требуется ручная проверка CAPTCHA")
                    job.pause_for_captcha(query, review_id)
                    await message.answer(
                        f"⏸ Массовая проверка приостановлена на компании {job.index + 1}/{len(job.queries)}.\n"
                        f"Реквизиты: {query.value}\n\n"
                        f"ФНС запросила CAPTCHA. Откройте официальный сервис, выполните проверку вручную "
                        f"для этой компании, затем нажмите кнопку ниже.\n"
                        f"Источник: {_batch_captcha_url()}\n\n"
                        f"Запрос не считается проверенным, пока ФНС не вернёт результат.",
                        reply_markup=_captcha_keyboard(job.job_id),
                    )
                    await job.wait_for_resume()
                    attempt = 0
                    continue
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
                    _record_batch_pending(job, query, f"не проверено после повторов: {error}")
                    await _record_batch_failure(job, query, error)
                    rows.append(row_from_error(query.value, f"требуется ручная проверка: {error}"))
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
                    _record_batch_pending(job, query, f"требуется ручная проверка: {error}")
                    await _record_batch_failure(job, query, error)
                    rows.append(row_from_error(query.value, f"требуется ручная проверка: {error}"))
                    await message.answer(f"❌ {query.value} — {error}")
                    break
                except Exception:
                    logging.exception("Ошибка пакетной проверки %s", query.value)
                    failed += 1
                    error = "внутренняя ошибка проверки"
                    _record_batch_pending(job, query, error)
                    await _record_batch_failure(job, query, error)
                    rows.append(row_from_error(query.value, error))
                    await message.answer(f"❌ {query.value} — {error}")
                    break
                else:
                    _resolve_batch_review(job, job.captcha_review_id)
                    job.clear_captcha_review()
                    event_id, _report = await _record_batch_success(message, job, query, result)
                    succeeded += 1
                    rows.append(row_from_assessment(result))
                    await message.answer(_bulk_result_line(query.value, event_id, result))
                    break

            if job.state is BatchJobState.CANCELLED:
                break
            job.index += 1

        if job.state is BatchJobState.CANCELLED:
            remaining = job.queries[job.index:]
            rows.extend(
                row_from_error(query.value, "проверка отменена пользователем")
                for query in remaining
            )
            await message.answer(
                f"⏹ Пакетная проверка отменена. Обработано: {job.index}/{len(job.queries)}."
            )
        else:
            job.complete()
            await message.answer(
                f"✅ Массовая проверка завершена: успешно — {succeeded}, с ошибкой — {failed}."
            )

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
        f"Задание: {job.job_id}. При CAPTCHA очередь остановится и продолжится после вашего подтверждения."
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


@router.callback_query(F.data.startswith("batch_captcha_resume:"))
async def batch_captcha_resume(callback: CallbackQuery) -> None:
    if not callback.message or not callback.from_user:
        await callback.answer("Не удалось определить чат.", show_alert=True)
        return
    job_id = (callback.data or "").split(":", 1)[1]
    job = batch_jobs.get(job_id)
    if (
        job is None
        or callback.message.chat.id != job.chat_id
        or callback.from_user.id != job.actor_id
        or not _can_check_user(callback.message.chat.id, callback.from_user)
    ):
        await callback.answer("У вас нет права продолжить это задание.", show_alert=True)
        return
    if job.resume_after_captcha():
        await callback.answer("Очередь возобновлена.")
        await callback.message.answer(
            f"▶️ Повторяю проверку компании {job.index + 1}/{len(job.queries)}."
        )
    else:
        await callback.answer("Это задание уже не ожидает CAPTCHA.", show_alert=True)


@router.callback_query(F.data.startswith("batch_captcha_cancel:"))
async def batch_captcha_cancel(callback: CallbackQuery) -> None:
    if not callback.message or not callback.from_user:
        await callback.answer("Не удалось определить чат.", show_alert=True)
        return
    job_id = (callback.data or "").split(":", 1)[1]
    job = batch_jobs.get(job_id)
    if (
        job is None
        or callback.message.chat.id != job.chat_id
        or callback.from_user.id != job.actor_id
        or not _can_check_user(callback.message.chat.id, callback.from_user)
    ):
        await callback.answer("У вас нет права отменить это задание.", show_alert=True)
        return
    if job.cancel():
        await callback.answer("Пакет отменён.")
        await callback.message.answer("⏹ Запрошена отмена пакетной проверки.")
    else:
        await callback.answer("Задание уже завершено.", show_alert=True)


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


@router.message(Command("captcha_done"))
async def captcha_done(message: Message, command: CommandObject) -> None:
    job = _batch_job_for_message(message, (command.args or "").strip())
    if job is None:
        await message.answer("Активной пакетной проверки, ожидающей CAPTCHA, нет.")
        return
    if job.resume_after_captcha():
        await message.answer("▶️ CAPTCHA отмечена как пройденная. Очередь продолжена.")
    else:
        await message.answer("Задание сейчас не ожидает CAPTCHA.")


@router.message(Command("batch_cancel"))
async def batch_cancel(message: Message, command: CommandObject) -> None:
    job = _batch_job_for_message(message, (command.args or "").strip())
    if job is None:
        await message.answer("Активной пакетной проверки в этом чате нет.")
        return
    job.cancel()
    await message.answer("⏹ Запрошена отмена пакетной проверки.")


@router.message(Command("batch_pending"))
async def batch_pending(message: Message, command: CommandObject) -> None:
    if not message.from_user or not _can_check(message) or batch_review_store is None:
        await message.answer("У вас нет разрешения на просмотр ручных проверок.")
        return
    try:
        limit = max(1, min(int((command.args or "20").strip()), 100))
    except ValueError:
        limit = 20
    entries = batch_review_store.list_pending(
        chat_id=message.chat.id,
        actor_id=message.from_user.id,
        is_root=_is_root(message),
        limit=limit,
    )
    if not entries:
        await message.answer("Компаний, ожидающих ручной проверки или повтора, нет.")
        return
    lines = ["Компании, требующие ручной проверки или повтора:"]
    lines.extend(f"• {entry.review_id} — {entry.query_text} — {entry.reason}" for entry in entries)
    await _answer_chunks(message, lines)


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
        payload = __import__("json").loads(message.web_app_data.data)
    except (TypeError, ValueError):
        await message.answer("Mini App передал некорректные данные.")
        return
    if payload.get("action") not in {"check", "watch"} or not isinstance(payload.get("query"), str):
        await message.answer("Неизвестное действие Mini App.")
        return
    if payload["action"] == "watch":
        if not _allowed(message) or not message.from_user or watch_store is None:
            await message.answer("У вас нет разрешения на мониторинг компаний.")
            return
        watch_store.add(message.chat.id, payload["query"], message.from_user.id)
        _audit(message, "watch_added", "company_added_from_mini_app")
        await message.answer("Компания добавлена в мониторинг.")
        return
    await _run_check(message, payload["query"])


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
    _audit(message, "learning_review", f"approved={parts[1] == 'approved'}")
    await message.answer("Запись обработана." if saved else "Запись не найдена.")


@router.message(Command("learning_export"))
async def learning_export(message: Message, command: CommandObject) -> None:
    if not message.from_user or not _is_root(message) or learning_store is None:
        await message.answer("Команда доступна администраторам.")
        return
    export_path = os.getenv("LEARNING_EXPORT_PATH", "data/learning.jsonl")
    count = learning_store.export_jsonl(export_path)
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
        role=_requested_role(command),
    ):
        _audit(message, "access_granted", f"role={_requested_role(command)}")
        await message.answer(f"Пользователю @{tag} выдан доступ в этом чате с ролью {_requested_role(command)}.")
    else:
        await message.answer("Нельзя выдать доступ Главному администратору или указан некорректный тег.")


@router.message(Command("revoke"))
async def revoke_access(message: Message, command: CommandObject) -> None:
    if not _is_root(message) or not message.from_user or access_store is None:
        await message.answer("Команда доступна Главному администратору @Sholomon.")
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
        await message.answer("Команда доступна Главному администратору @Sholomon.")
        return
    users = access_store.list_trusted(message.chat.id)
    if not users:
        await message.answer("В этом чате нет выданных прав. Главный администратор: @Sholomon.")
        return
    lines = ["Доверенные пользователи этого чата:", "@Sholomon — Главный администратор"]
    lines.extend(f"@{user.tag} — {user.role}" for user in users)
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
            + skills_text()
        )
    elif request.intent is NaturalIntent.GREETING:
        await message.answer(
            "Здравствуйте. Я готов проверить юридическое лицо по ИНН, ОГРН, КПП, "
            "названию, адресу или другим данным. Например: «Налог, проверь компанию с ИНН 7707083893»."
        )
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
                                    await asyncio.sleep(2 ** attempt)
                        if result is None:
                            continue
                    record = result.record
                    snapshot = "|".join(str(item) for item in (
                        record.name, record.inn, record.ogrn, record.kpp,
                        record.status, record.inaccuracy_state.value,
                        ",".join(record.inaccuracy_markers),
                    ))
                    if watch_store.update_snapshot(chat_id, raw_query, snapshot):
                        await bot.send_message(
                            int(chat_id),
                            "⚠️ Изменились данные компании в мониторинге.\n\n" + format_assessment(result),
                        )
            if license_store:
                for license_record in license_store.expiring(_positive_env_int("LICENSE_ALERT_DAYS", 30)):
                    days = license_record.days_left()
                    # Лицензионные уведомления направляются в чаты, где есть
                    # компании из watchlist и совпадает ИНН.
                    if watch_store:
                        for chat_id, raw_query, _ in watch_store.entries():
                            if license_record.inn not in raw_query:
                                continue
                            key = (str(chat_id), license_record.license_id,
                                   str(license_record.expires_at), str(days))
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


def _telegram_proxy() -> str | None:
    """Возвращает проверенный прокси для Telegram API без вывода секрета в логи."""

    value = os.getenv("TELEGRAM_PROXY", "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme.casefold() not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise RuntimeError(
            "TELEGRAM_PROXY должен быть URL HTTPS/HTTP или SOCKS5-прокси, например "
            "socks5://user:password@host:1080."
        )
    return value


async def _run() -> None:
    global agent, access_store, learning_store, history_store, license_store, audit_store, watch_store, batch_review_store, chat_skill
    global reaction_settings
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN в .env")
    hash_salt = os.getenv("LEARNING_HASH_SALT", "").strip()
    if not hash_salt:
        raise RuntimeError(
            "Не задан LEARNING_HASH_SALT: укажите стабильную случайную соль для защиты идентификаторов."
        )
    access_store = ChatAccessStore(
        os.getenv("ACCESS_DB_PATH", "data/access.sqlite3"),
    )
    fns_client = FnsEgrulClient(
        base_url=os.getenv("FNS_BASE_URL", "").strip() or "https://egrul.nalog.ru/",
        timeout=_positive_env_float("FNS_TIMEOUT_SECONDS", 20.0),
    )
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
    batch_review_store = BatchReviewStore(
        os.getenv("BATCH_REVIEW_DB_PATH", "data/batch_review.sqlite3"),
        hash_salt=hash_salt,
    )
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
    monitor_task = asyncio.create_task(
        _monitor_loop(bot, _positive_env_int("MONITOR_INTERVAL_SECONDS", 86400))
    )
    try:
        await dispatcher.start_polling(bot)
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
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
        if batch_review_store:
            batch_review_store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())
