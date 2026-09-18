from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass


MAIN_ADMIN_TAG = "sholomon"


def normalize_tag(value: str) -> str:
    return value.strip().lstrip("@").casefold()


def parse_tags(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(normalize_tag(item) for item in value.split(",") if item.strip())


def is_main_admin(username: str | None) -> bool:
    return bool(username) and normalize_tag(username or "") == MAIN_ADMIN_TAG


class AccessPolicy:
    """Первый слой доступа Telegram по username-тегам.

    В рабочей версии рекомендуется дополнительно закрепить Telegram user_id:
    username пользователь может изменить, а user_id является стабильным.
    """

    def __init__(self, allowed_tags: frozenset[str], admin_tags: frozenset[str] = frozenset()) -> None:
        self.allowed_tags = allowed_tags | admin_tags
        self.admin_tags = admin_tags

    def is_allowed(self, username: str | None) -> bool:
        return bool(username) and normalize_tag(username or "") in self.allowed_tags

    def is_admin(self, username: str | None) -> bool:
        return bool(username) and normalize_tag(username or "") in self.admin_tags


@dataclass(frozen=True, slots=True)
class TrustedUser:
    tag: str
    user_id: str | None
    status: str
    updated_at: str


class ChatAccessStore:
    """Постоянный список доверенных пользователей по каждому Telegram-чату."""

    def __init__(
        self,
        path: str | Path = "data/access.sqlite3",
        *,
        bootstrap_tags: frozenset[str] = frozenset(),
    ) -> None:
        self.path = str(path)
        self.bootstrap_tags = bootstrap_tags - {MAIN_ADMIN_TAG}
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
                CREATE TABLE IF NOT EXISTS chat_access (
                    chat_id TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    user_id TEXT,
                    status TEXT NOT NULL CHECK(status IN ('granted', 'revoked')),
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    PRIMARY KEY (chat_id, tag)
                )
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def is_allowed(self, chat_id: int | str, *, user_id: int | str | None, username: str | None) -> bool:
        if is_main_admin(username):
            return True
        tag = normalize_tag(username or "") if username else ""
        user_key = str(user_id) if user_id is not None else None
        with self._lock:
            row = self._connection.execute(
                """
                SELECT status FROM chat_access
                WHERE chat_id = ? AND (tag = ? OR (user_id IS NOT NULL AND user_id = ?))
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (str(chat_id), tag, user_key),
            ).fetchone()
        if row is not None:
            if row["status"] == "granted" and user_key and tag:
                with self._lock, self._connection:
                    self._connection.execute(
                        """
                        UPDATE chat_access SET user_id = ?
                        WHERE chat_id = ? AND tag = ? AND status = 'granted' AND user_id IS NULL
                        """,
                        (user_key, str(chat_id), tag),
                    )
            return row["status"] == "granted"
        return bool(tag and tag in self.bootstrap_tags)

    def grant(
        self,
        chat_id: int | str,
        *,
        username: str,
        user_id: int | str | None,
        granted_by: int | str,
    ) -> bool:
        tag = normalize_tag(username)
        if not tag or tag == MAIN_ADMIN_TAG:
            return False
        now = self._now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO chat_access (chat_id, tag, user_id, status, updated_at, updated_by)
                VALUES (?, ?, ?, 'granted', ?, ?)
                ON CONFLICT(chat_id, tag) DO UPDATE SET
                    user_id = excluded.user_id,
                    status = 'granted',
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (str(chat_id), tag, str(user_id) if user_id is not None else None, now, str(granted_by)),
            )
        return True

    def revoke(
        self,
        chat_id: int | str,
        *,
        username: str,
        revoked_by: int | str,
    ) -> bool:
        tag = normalize_tag(username)
        if not tag or tag == MAIN_ADMIN_TAG:
            return False
        now = self._now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO chat_access (chat_id, tag, user_id, status, updated_at, updated_by)
                VALUES (?, ?, NULL, 'revoked', ?, ?)
                ON CONFLICT(chat_id, tag) DO UPDATE SET
                    user_id = COALESCE(chat_access.user_id, excluded.user_id),
                    status = 'revoked',
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (str(chat_id), tag, now, str(revoked_by)),
            )
        return True

    def list_trusted(self, chat_id: int | str) -> list[TrustedUser]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT tag, user_id, status, updated_at
                FROM chat_access
                WHERE chat_id = ? AND status = 'granted'
                ORDER BY tag
                """,
                (str(chat_id),),
            ).fetchall()
        return [
            TrustedUser(
                tag=row["tag"],
                user_id=row["user_id"],
                status=row["status"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]
