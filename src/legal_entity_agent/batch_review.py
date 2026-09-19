"""Постоянный список компаний, ожидающих ручной проверки.

Записи не означают положительный или отрицательный результат ФНС. Они нужны
только для того, чтобы после остановки процесса не потерять компанию, которую
нужно проверить вручную или повторить после устранения временной ошибки.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BatchReviewEntry:
    review_id: str
    chat_id: str
    query_text: str
    status: str
    reason: str
    source: str
    created_at: datetime
    updated_at: datetime


class BatchReviewStore:
    """SQLite-хранилище незавершённых элементов массовой проверки."""

    def __init__(self, path: str = "data/batch_review.sqlite3", *, hash_salt: str = "") -> None:
        self.path = str(path)
        self.hash_salt = hash_salt
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS batch_review (
                    review_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    actor_hash TEXT NOT NULL,
                    query_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_review_chat_status "
                "ON batch_review(chat_id, status, updated_at)"
            )

    def record_pending(
        self,
        *,
        review_id: str,
        chat_id: int | str,
        actor_id: int | str,
        query_text: str,
        reason: str,
        source: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO batch_review(
                    review_id, chat_id, actor_hash, query_text, status, reason,
                    source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                ON CONFLICT(review_id) DO UPDATE SET
                    reason = excluded.reason,
                    status = 'pending',
                    updated_at = excluded.updated_at
                """,
                (
                    review_id,
                    str(chat_id),
                    self._digest(str(actor_id)),
                    query_text,
                    reason[:500],
                    source,
                    now,
                    now,
                ),
            )

    def resolve(self, review_id: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE batch_review SET status='resolved', updated_at=? "
                "WHERE review_id=? AND status='pending'",
                (datetime.now(UTC).isoformat(), review_id),
            )
        return cursor.rowcount > 0

    def list_pending(
        self,
        *,
        chat_id: int | str,
        actor_id: int | str,
        is_root: bool,
        limit: int = 20,
    ) -> list[BatchReviewEntry]:
        sql = "SELECT * FROM batch_review WHERE chat_id=? AND status='pending'"
        params: list[object] = [str(chat_id)]
        if not is_root:
            sql += " AND actor_hash=?"
            params.append(self._digest(str(actor_id)))
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(limit, 100)))
        with self._lock:
            rows = self._db.execute(sql, tuple(params)).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _digest(self, value: str) -> str:
        return hmac.new(self.hash_salt.encode(), value.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> BatchReviewEntry:
        return BatchReviewEntry(
            review_id=row["review_id"],
            chat_id=row["chat_id"],
            query_text=row["query_text"],
            status=row["status"],
            reason=row["reason"],
            source=row["source"],
            created_at=datetime.fromisoformat(row["created_at"]).astimezone(UTC),
            updated_at=datetime.fromisoformat(row["updated_at"]).astimezone(UTC),
        )
