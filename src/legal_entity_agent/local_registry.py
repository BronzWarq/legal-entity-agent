"""Read-only adapter for a local SQLite EGRUL/EGRIP snapshot.

The snapshot is produced by the official FNS data integration/import process
and is optional.  The adapter never treats a missing local record as a
negative FNS result; it only returns records that are present in the snapshot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .identifiers import parse_search_query
from .models import FnsEntityRecord, Identifier, InaccuracyState, SearchQuery

logger = logging.getLogger(__name__)

FNS_INTEGRATION_URL = "https://www.nalog.gov.ru/rn77/service/egrip2/"
_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+", re.UNICODE)
_COMPANY_FIELDS = {"ИНН": "inn", "ОГРН": "ogrn", "КПП": "kpp"}
_IE_FIELDS = {"ИНН": "inn", "ОГРН": "ogrnip"}
_STATUS_LABELS = {
    "active": "действует",
    "reorganizing": "в стадии реорганизации",
    "liquidating": "в стадии ликвидации",
    "liquidated": "деятельность прекращена",
    "bankrupt": "банкротство",
    "closed": "деятельность прекращена",
}


class LocalEgrulClient:
    """Ищет точные реквизиты в SQLite-базе `atomno-mcp-egrul`.

    Соединение открывается в режиме read-only на каждый запрос. Это позволяет
    импортёру обновлять базу отдельно от процесса Telegram-бота.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def lookup(self, value: str | Identifier | SearchQuery) -> FnsEntityRecord | None:
        if isinstance(value, SearchQuery):
            query = value
        elif isinstance(value, Identifier):
            query = SearchQuery(value.value, (value,))
        else:
            query = parse_search_query(value)
        return await asyncio.to_thread(self._lookup_sync, query)

    def _lookup_sync(self, query: SearchQuery) -> FnsEntityRecord | None:
        if not self.path.is_file():
            return None
        try:
            with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                row, entity_type = self._lookup_by_identifiers(connection, query)
                if row is None and not query.identifiers:
                    row, entity_type = self._lookup_by_name(connection, query.value)
                return self._to_record(row, entity_type, query) if row is not None else None
        except sqlite3.Error:
            logger.warning("Не удалось прочитать локальную базу ЕГРЮЛ/ЕГРИП: %s", self.path, exc_info=True)
            return None

    @staticmethod
    def _lookup_by_identifiers(connection: sqlite3.Connection, query: SearchQuery):
        company_conditions = []
        company_values: list[str] = []
        ie_conditions = []
        ie_values: list[str] = []
        for identifier in query.identifiers:
            company_field = _COMPANY_FIELDS.get(identifier.kind.value)
            if company_field:
                company_conditions.append(f"{company_field} = ?")
                company_values.append(identifier.value)
            ie_field = _IE_FIELDS.get(identifier.kind.value)
            if ie_field:
                ie_conditions.append(f"{ie_field} = ?")
                ie_values.append(identifier.value)

        if company_conditions:
            row = connection.execute(
                f"SELECT * FROM companies WHERE {' AND '.join(company_conditions)} LIMIT 1",
                tuple(company_values),
            ).fetchone()
            if row is not None:
                return row, "company"
        if ie_conditions:
            row = connection.execute(
                f"SELECT * FROM individual_entrepreneurs WHERE {' AND '.join(ie_conditions)} LIMIT 1",
                tuple(ie_values),
            ).fetchone()
            if row is not None:
                return row, "ie"
        return None, None

    @staticmethod
    def _lookup_by_name(connection: sqlite3.Connection, value: str):
        tokens = [token.replace('"', "") for token in _TOKEN_RE.findall(value) if len(token) >= 2]
        if not tokens:
            return None, None
        fts_query = " AND ".join(f'"{token}"' for token in tokens)
        try:
            row = connection.execute(
                """
                SELECT c.*
                FROM companies_fts AS f
                JOIN companies AS c ON c.rowid = f.rowid
                WHERE companies_fts MATCH ?
                ORDER BY bm25(companies_fts)
                LIMIT 1
                """,
                (fts_query,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        return (row, "company") if row is not None else (None, None)

    @staticmethod
    def _to_record(row: sqlite3.Row, entity_type: str | None, query: SearchQuery) -> FnsEntityRecord:
        keys = set(row.keys())

        def value(name: str, default: Any = None) -> Any:
            return row[name] if name in keys else default

        data = value("data_json", "{}")
        try:
            raw_data = json.loads(data) if isinstance(data, str) else data
        except (TypeError, ValueError):
            raw_data = {}
        if not isinstance(raw_data, dict):
            raw_data = {}
        source_date = value("source_date") or "unknown"
        raw = {**raw_data, "local_snapshot": True, "source_date": source_date, "entity_type": entity_type}
        name = value("name_full") or value("name_short") or value("fio")
        ogrn = value("ogrn") or value("ogrnip")
        registration_date = _parse_date(value("registered_at"))
        status = _STATUS_LABELS.get(str(value("status") or "").casefold(), value("status"))
        return FnsEntityRecord(
            query=query,
            source_url=FNS_INTEGRATION_URL,
            fetched_at=datetime.now(UTC),
            name=name,
            inn=value("inn"),
            ogrn=ogrn,
            kpp=value("kpp"),
            status=status,
            registration_date=registration_date,
            address=value("address_legal"),
            inaccuracy_state=InaccuracyState.NOT_REPORTED,
            raw=raw,
        )


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
