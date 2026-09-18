"""Безопасный контур обратной связи и подготовки данных для обучения.

Модуль намеренно не изменяет поведение агента автоматически. Запросы и оценки
сначала попадают в журнал, затем проходят проверку администратора и лишь после
этого экспортируются в набор данных для изменения правил или модели.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import Assessment, Identifier, SearchQuery


class FeedbackLabel(StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class LearningEvent:
    event_id: str
    created_at: datetime
    actor_hash: str
    query_kind: str
    query_hash: str
    response_state: str
    source_url: str
    raw_query: str | None = None
    rendered_response: str | None = None
    feedback: FeedbackLabel | None = None
    feedback_note: str | None = None
    reviewed: bool = False
    review_decision: str | None = None
    correction: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _digest(value: str, salt: str) -> str:
    """Возвращает стабильный HMAC-отпечаток без сохранения исходного значения."""

    key = salt.encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _as_query(value: Identifier | SearchQuery) -> SearchQuery:
    if isinstance(value, SearchQuery):
        return value
    return SearchQuery(value.value, (value,))


def _query_kind(query: SearchQuery) -> str:
    if not query.identifiers:
        return "свободный текст"
    return "+".join(dict.fromkeys(item.kind.value for item in query.identifiers))


class LearningStore:
    """SQLite-журнал запросов и проверенных обратных связей.

    По умолчанию исходные реквизиты и текст ответа не сохраняются. Для
    подготовки датасета с исходными данными это можно включить явно через
    ``store_raw=True`` после принятия внутренних правил хранения данных.
    """

    def __init__(
        self,
        path: str | Path = "data/learning.sqlite3",
        *,
        store_raw: bool = False,
        hash_salt: str = "",
    ) -> None:
        self.path = str(path)
        self.store_raw = store_raw
        self.hash_salt = hash_salt
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_events (
                    event_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    actor_hash TEXT NOT NULL,
                    query_kind TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    response_state TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    raw_query TEXT,
                    rendered_response TEXT,
                    feedback TEXT,
                    feedback_note TEXT,
                    feedback_at TEXT,
                    reviewed INTEGER NOT NULL DEFAULT 0,
                    reviewed_at TEXT,
                    reviewer_hash TEXT,
                    review_decision TEXT,
                    correction TEXT
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_learning_events_reviewed "
                "ON learning_events (reviewed, created_at)"
            )

    def record_check(
        self,
        *,
        actor_id: str,
        identifier: Identifier | SearchQuery,
        assessment: Assessment,
        rendered_response: str | None = None,
    ) -> str:
        query = _as_query(identifier)
        event_id = secrets.token_urlsafe(9)
        created_at = _utc_now()
        raw_query = query.value if self.store_raw else None
        answer = rendered_response if self.store_raw else None
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO learning_events (
                    event_id, created_at, actor_hash, query_kind, query_hash,
                    response_state, source_url, raw_query, rendered_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    _timestamp(created_at),
                    _digest(actor_id, self.hash_salt),
                    _query_kind(query),
                    _digest(query.value, self.hash_salt),
                    assessment.record.inaccuracy_state.value,
                    assessment.record.source_url,
                    raw_query,
                    answer,
                ),
            )
        return event_id

    def record_failure(
        self,
        *,
        actor_id: str,
        identifier: Identifier | SearchQuery,
        error: str,
    ) -> str:
        """Фиксирует неуспешный запрос без сохранения текста ошибки по умолчанию."""

        query = _as_query(identifier)
        event_id = secrets.token_urlsafe(9)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO learning_events (
                    event_id, created_at, actor_hash, query_kind, query_hash,
                    response_state, source_url, raw_query, rendered_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    _timestamp(_utc_now()),
                    _digest(actor_id, self.hash_salt),
                    _query_kind(query),
                    _digest(query.value, self.hash_salt),
                    "error",
                    "",
                    query.value if self.store_raw else None,
                    error if self.store_raw else None,
                ),
            )
        return event_id

    def add_feedback(
        self,
        *,
        event_id: str,
        actor_id: str,
        label: FeedbackLabel,
        note: str | None = None,
        is_admin: bool = False,
    ) -> bool:
        """Добавляет оценку владельца запроса или администратора.

        Пользователь может оценить лишь собственный запрос. Администратор может
        скорректировать оценку при разборе очереди.
        """

        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT actor_hash FROM learning_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return False
            if not is_admin and not hmac.compare_digest(row["actor_hash"], _digest(actor_id, self.hash_salt)):
                return False
            self._connection.execute(
                """
                UPDATE learning_events
                SET feedback = ?, feedback_note = ?, feedback_at = ?
                WHERE event_id = ?
                """,
                (label.value, note, _timestamp(_utc_now()), event_id),
            )
        return True

    def pending(self, limit: int = 20) -> list[LearningEvent]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM learning_events
                WHERE reviewed = 0
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def review(
        self,
        *,
        event_id: str,
        reviewer_id: str,
        approved: bool,
        correction: str | None = None,
    ) -> bool:
        """Помечает запись проверенной человеком.

        ``approved=False`` сохраняет запись в журнале как отклонённую, но она
        не попадает в выгрузку одобренных примеров.
        """

        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE learning_events
                SET reviewed = 1, reviewed_at = ?, reviewer_hash = ?,
                    review_decision = ?, correction = ?
                WHERE event_id = ?
                """,
                (
                    _timestamp(_utc_now()),
                    _digest(reviewer_id, self.hash_salt),
                    "approved" if approved else "rejected",
                    correction if approved else None,
                    event_id,
                ),
            )
        return cursor.rowcount == 1

    def export_jsonl(self, path: str | Path, *, reviewed_only: bool = True) -> int:
        """Экспортирует одобренные примеры для последующей проверки/обучения."""

        clause = "WHERE reviewed = 1 AND review_decision = 'approved'" if reviewed_only else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM learning_events {clause} ORDER BY created_at ASC"
            ).fetchall()
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as stream:
            for row in rows:
                item: dict[str, Any] = {
                    "event_id": row["event_id"],
                    "created_at": row["created_at"],
                    "query_kind": row["query_kind"],
                    "query_hash": row["query_hash"],
                    "response_state": row["response_state"],
                    "source_url": row["source_url"],
                    "feedback": row["feedback"],
                    "feedback_note": row["feedback_note"],
                    "review_decision": row["review_decision"],
                    "correction": row["correction"],
                }
                if self.store_raw:
                    item["query"] = row["raw_query"]
                    item["rendered_response"] = row["rendered_response"]
                else:
                    item["query"] = None
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        return len(rows)

    def purge_older_than(self, days: int) -> int:
        """Удаляет старые записи для выполнения внутренней политики хранения."""

        if days < 1:
            raise ValueError("Срок хранения должен быть не менее одного дня.")
        cutoff = _timestamp(_utc_now() - timedelta(days=days))
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM learning_events WHERE created_at < ?", (cutoff,)
            )
        return cursor.rowcount

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> LearningEvent:
        feedback = row["feedback"]
        return LearningEvent(
            event_id=row["event_id"],
            created_at=_parse_timestamp(row["created_at"]),
            actor_hash=row["actor_hash"],
            query_kind=row["query_kind"],
            query_hash=row["query_hash"],
            response_state=row["response_state"],
            source_url=row["source_url"],
            raw_query=row["raw_query"],
            rendered_response=row["rendered_response"],
            feedback=FeedbackLabel(feedback) if feedback else None,
            feedback_note=row["feedback_note"],
            reviewed=bool(row["reviewed"]),
            review_decision=row["review_decision"],
            correction=row["correction"],
        )
