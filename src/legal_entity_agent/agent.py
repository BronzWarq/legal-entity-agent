from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import Any

from .fns_client import FnsEgrulClient, FnsLookupClient
from .identifiers import parse_search_query
from .models import Assessment, SearchQuery

logger = logging.getLogger(__name__)


class LegalEntityAgent:
    """Оркестратор проверки юридического лица."""

    def __init__(
        self,
        client: FnsLookupClient | None = None,
        deep_checker: Any | None = None,
    ) -> None:
        self.client = client or FnsEgrulClient()
        self.deep_checker = deep_checker

    async def check(self, value: str | SearchQuery) -> Assessment:
        query = value if isinstance(value, SearchQuery) else parse_search_query(value)
        record = await self.client.lookup(query)
        assessment = Assessment(record)
        if self.deep_checker is None:
            return assessment

        # Upstream принимает один ИНН/ОГРН. Если пользователь указал несколько
        # разных идентификаторов, не выбираем один молча и не создаём ложный
        # результат расширенной проверки.
        deep_identifiers = [
            item.value
            for item in query.identifiers
            if item.kind.value in {"ИНН", "ОГРН"}
        ]
        if len(set(deep_identifiers)) != 1:
            return Assessment(
                record,
                deep_check_error=(
                    "Расширенная проверка пропущена: нужен ровно один ИНН или ОГРН."
                )
                if len(set(deep_identifiers)) > 1
                else "Расширенная проверка пропущена: в запросе нет ИНН или ОГРН.",
            )
        try:
            deep_result = await self.deep_checker.check(deep_identifiers[0])
            payload = getattr(deep_result, "report", deep_result)
            if not isinstance(payload, dict):
                raise TypeError("отчёт не является объектом JSON")
            return Assessment(record, deep_report=payload)
        except Exception as exc:  # расширенный источник best-effort
            logger.warning("Расширенная проверка не выполнена: %s", exc)
            return Assessment(record, deep_check_error=str(exc))

    async def close(self) -> None:
        close = getattr(self.deep_checker, "close", None)
        if close is not None:
            result = close()
            if isinstance(result, Awaitable):
                await result
