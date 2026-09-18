"""Необязательная интеграция с atomno-mcp-fns-check.

Upstream-проект поставляет MCP-сервер, поэтому его публичный CLI неудобен для
вызова из Telegram-бота. Здесь используется его Python-бизнес-логика напрямую
и только если установлен optional extra ``[deep-check]``.

Важное свойство: кэш сырых ответов upstream заменён одноразовым no-op кэшем.
Это сохраняет проверки источников, но не записывает ответы ФНС на диск и не
создаёт ложного «чисто» при недоступном источнике.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any


class DeepCheckError(RuntimeError):
    """Расширенная проверка не выполнена или вернула неожиданный ответ."""


class DeepCheckUnavailable(DeepCheckError):
    """Пакет расширенной проверки не установлен."""


class _EphemeralCache:
    """Совместимый с upstream cache, который ничего не сохраняет."""

    async def init(self) -> None:
        return None

    async def get(self, key: str, source: str) -> None:
        return None

    async def put(
        self,
        key: str,
        source: str,
        payload: dict[str, Any],
        *,
        ttl_hours: int | None = None,
    ) -> None:
        return None


@dataclass(frozen=True, slots=True)
class DeepCheckResult:
    """Структурированный отчёт atomno-mcp-fns-check без сырых payload."""

    report: dict[str, Any]
    source: str = "atomno-mcp-fns-check"


class AtomnoFnsCheckAdapter:
    """Адаптер к агрегатору ``check_contractor`` upstream-проекта.

    Контекст создаётся лениво при первом вызове и переиспользуется в процессе.
    ``close`` вызывается при завершении бота/CLI.
    """

    def __init__(
        self,
        *,
        include_extended_risks: bool = True,
        lawsuits_threshold_rub: float = 1_000_000.0,
    ) -> None:
        self.include_extended_risks = include_extended_risks
        self.lawsuits_threshold_rub = lawsuits_threshold_rub
        self._context: Any | None = None

    async def _get_context(self) -> Any:
        if self._context is not None:
            return self._context
        try:
            context_module = importlib.import_module("atomno_mcp_fns_check.context")
            source_context = context_module.ServiceContext.from_env()
        except (ImportError, ModuleNotFoundError) as exc:
            raise DeepCheckUnavailable(
                "Пакет atomno-mcp-fns-check не установлен. "
                "Установите зависимости командой: pip install -e '.[deep-check]'"
            ) from exc

        # Не позволяем upstream записывать сырые ответы ЕГРЮЛ в SQLite-кэш.
        source_context.cache = _EphemeralCache()
        try:
            await source_context.__aenter__()
        except Exception:
            # Контекст может успеть открыть часть HTTP-клиентов до ошибки
            # (например, из-за неподдержанного proxy). Освобождаем их best-effort.
            for name in ("egrul", "efrsb", "pb_fns", "fssp", "kad", "disqualified"):
                client = getattr(source_context, name, None)
                close = getattr(client, "__aexit__", None)
                if close is not None:
                    try:
                        await close(None, None, None)
                    except Exception:
                        pass
            raise
        self._context = source_context
        return source_context

    async def check(self, identifier: str) -> DeepCheckResult:
        """Проверить один ИНН/ОГРН и вернуть JSON-совместимый отчёт."""

        try:
            contractor = importlib.import_module("atomno_mcp_fns_check.tools.contractor")
        except (ImportError, ModuleNotFoundError) as exc:
            raise DeepCheckUnavailable(
                "Пакет atomno-mcp-fns-check не установлен. "
                "Установите зависимости командой: pip install -e '.[deep-check]'"
            ) from exc
        context = await self._get_context()
        try:
            report = await contractor.check_contractor(
                context,
                identifier,
                include_extended_risks=self.include_extended_risks,
                lawsuits_threshold_rub=self.lawsuits_threshold_rub,
            )
        except Exception as exc:
            # Ошибка upstream не должна превращаться в «рисков нет».
            raise DeepCheckError(f"Расширенная проверка не выполнена: {exc}") from exc
        if hasattr(report, "model_dump"):
            payload = report.model_dump(mode="json")
        elif isinstance(report, dict):
            payload = report
        else:
            raise DeepCheckError("Расширенный источник вернул неожиданный формат отчёта.")
        return DeepCheckResult(report=payload)

    async def close(self) -> None:
        if self._context is not None:
            await self._context.__aexit__(None, None, None)
            self._context = None
