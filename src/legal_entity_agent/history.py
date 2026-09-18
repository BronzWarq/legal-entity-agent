"""История выполненных проверок юридических лиц."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from .models import Assessment, Identifier, SearchQuery


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    event_id: str
    chat_id: str
    created_at: datetime
    query_text: str
    query_kind: str
    name: str | None
    inn: str | None
    kpp: str | None
    ogrn: str | None
    status: str | None
    address: str | None
    registration_date: date | None
    inaccuracy_state: str
    inaccuracy_markers: tuple[str, ...]
    source_url: str
    report: str


def _digest(value: str, salt: str) -> str:
    return hmac.new(salt.encode(), value.encode(), hashlib.sha256).hexdigest()


def _query(value: Identifier | SearchQuery) -> SearchQuery:
    if isinstance(value, SearchQuery):
        return value
    return SearchQuery(value.value, (value,))


def _query_kind(query: SearchQuery) -> str:
    if not query.identifiers:
        return "свободный текст"
    return "+".join(dict.fromkeys(item.kind.value for item in query.identifiers))


class HistoryStore:
    """Хранит структурированный результат проверки без сырого ответа ФНС."""

    def __init__(self, path: str | Path = "data/history.sqlite3", *, hash_salt: str = "") -> None:
        self.path = str(path)
        self.hash_salt = hash_salt
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS check_history (
                    event_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    actor_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    query_text TEXT NOT NULL,
                    query_kind TEXT NOT NULL,
                    name TEXT,
                    inn TEXT,
                    kpp TEXT,
                    ogrn TEXT,
                    status TEXT,
                    address TEXT,
                    registration_date TEXT,
                    inaccuracy_state TEXT NOT NULL,
                    inaccuracy_markers TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    report TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_check_history_chat_time "
                "ON check_history (chat_id, created_at)"
            )

    def record_check(
        self,
        *,
        event_id: str,
        chat_id: int | str,
        actor_id: int | str,
        query: Identifier | SearchQuery,
        assessment: Assessment,
        report: str,
    ) -> None:
        normalized = _query(query)
        record = assessment.record
        created_at = datetime.now(UTC)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO check_history (
                    event_id, chat_id, actor_hash, created_at, query_text, query_kind,
                    name, inn, kpp, ogrn, status, address, registration_date,
                    inaccuracy_state, inaccuracy_markers, source_url, report
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    str(chat_id),
                    _digest(str(actor_id), self.hash_salt),
                    created_at.isoformat(),
                    normalized.value,
                    _query_kind(normalized),
                    record.name,
                    record.inn,
                    record.kpp,
                    record.ogrn,
                    record.status,
                    record.address,
                    record.registration_date.isoformat() if record.registration_date else None,
                    record.inaccuracy_state.value,
                    json.dumps(record.inaccuracy_markers, ensure_ascii=False),
                    record.source_url,
                    report,
                ),
            )

    def list_for(
        self,
        *,
        chat_id: int | str,
        actor_id: int | str,
        is_root: bool,
        limit: int = 20,
    ) -> list[HistoryEntry]:
        if is_root:
            sql = "SELECT * FROM check_history WHERE chat_id = ? ORDER BY created_at DESC LIMIT ?"
            params = (str(chat_id), max(1, min(limit, 100)))
        else:
            sql = (
                "SELECT * FROM check_history WHERE chat_id = ? AND actor_hash = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params = (str(chat_id), _digest(str(actor_id), self.hash_salt), max(1, min(limit, 100)))
        with self._lock:
            rows = self._connection.execute(sql, params).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def list_unique_queries(self) -> list[SearchQuery]:
        """Возвращает уникальные юридические лица из всей истории.

        Приоритетом для объединения записей служат ИНН и ОГРН из уже
        полученного ответа ФНС. КПП без ИНН/ОГРН не считается уникальным
        идентификатором, поэтому в этом случае используется исходный текст
        запроса. Более свежая запись сохраняется первой.
        """

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT query_text, inn, ogrn, kpp
                FROM check_history
                ORDER BY created_at DESC
                """
            ).fetchall()

        queries: list[SearchQuery] = []
        seen: set[str] = set()
        for row in rows:
            identities = [
                identity
                for identity in (
                    f"inn:{row['inn']}" if row["inn"] else None,
                    f"ogrn:{row['ogrn']}" if row["ogrn"] else None,
                )
                if identity is not None
            ]
            if not identities:
                identities = [f"query:{' '.join(row['query_text'].split()).casefold()}"]
            if any(identity in seen for identity in identities):
                continue
            seen.update(identities)
            queries.append(SearchQuery(row["query_text"]))
        return queries

    def get_for(
        self,
        *,
        event_id: str,
        chat_id: int | str,
        actor_id: int | str,
        is_root: bool,
    ) -> HistoryEntry | None:
        sql = "SELECT * FROM check_history WHERE event_id = ? AND chat_id = ?"
        params: tuple[str, ...] = (event_id, str(chat_id))
        if not is_root:
            sql += " AND actor_hash = ?"
            params += (_digest(str(actor_id), self.hash_salt),)
        with self._lock:
            row = self._connection.execute(sql, params).fetchone()
        return self._row_to_entry(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> HistoryEntry:
        registration_date = date.fromisoformat(row["registration_date"]) if row["registration_date"] else None
        return HistoryEntry(
            event_id=row["event_id"],
            chat_id=row["chat_id"],
            created_at=datetime.fromisoformat(row["created_at"]).astimezone(UTC),
            query_text=row["query_text"],
            query_kind=row["query_kind"],
            name=row["name"],
            inn=row["inn"],
            kpp=row["kpp"],
            ogrn=row["ogrn"],
            status=row["status"],
            address=row["address"],
            registration_date=registration_date,
            inaccuracy_state=row["inaccuracy_state"],
            inaccuracy_markers=tuple(json.loads(row["inaccuracy_markers"])),
            source_url=row["source_url"],
            report=row["report"],
        )
