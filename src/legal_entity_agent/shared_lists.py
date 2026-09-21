"""Временные ссылки для передачи списка компаний внутри чата."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SharedList:
    share_id: str
    chat_id: str
    owner_id: str
    queries: tuple[str, ...]
    expires_at: datetime


class SharedListStore:
    """Хранит временные списки, которые можно принять только в исходном чате."""

    def __init__(self, path: str = "data/shared_lists.sqlite3", *, ttl_hours: int = 24) -> None:
        self._lock = threading.RLock()
        self._ttl = timedelta(hours=max(1, ttl_hours))
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS shared_lists(
                    share_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    queries TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )

    def create(self, chat_id: int | str, owner_id: int | str, queries: list[str]) -> SharedList:
        normalized = tuple(dict.fromkeys(query.strip() for query in queries if query.strip()))
        if not normalized:
            raise ValueError("Список компаний пуст.")
        if len(normalized) > 100:
            raise ValueError("За один раз можно передать не более 100 компаний.")
        now = datetime.now(UTC)
        shared = SharedList(
            share_id=secrets.token_urlsafe(12),
            chat_id=str(chat_id),
            owner_id=str(owner_id),
            queries=normalized,
            expires_at=now + self._ttl,
        )
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO shared_lists(share_id, chat_id, owner_id, queries, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    shared.share_id,
                    shared.chat_id,
                    shared.owner_id,
                    json.dumps(shared.queries, ensure_ascii=False),
                    now.isoformat(),
                    shared.expires_at.isoformat(),
                ),
            )
        return shared

    def get(self, share_id: str, chat_id: int | str) -> SharedList | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM shared_lists WHERE share_id=? AND chat_id=?",
                (share_id, str(chat_id)),
            ).fetchone()
            if row is None:
                return None
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at <= datetime.now(UTC):
                with self._db:
                    self._db.execute("DELETE FROM shared_lists WHERE share_id=?", (share_id,))
                return None
            queries = tuple(json.loads(row["queries"]))
            return SharedList(
                share_id=row["share_id"],
                chat_id=row["chat_id"],
                owner_id=row["owner_id"],
                queries=queries,
                expires_at=expires_at,
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

