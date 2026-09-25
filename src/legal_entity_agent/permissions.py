from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def normalize_tag(value: str) -> str:
    return value.strip().lstrip("@").casefold()


def parse_tags(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(normalize_tag(item) for item in value.split(",") if item.strip())


def configured_main_admin_user_id() -> str | None:
    """Return the configured immutable Telegram user id, if valid."""

    value = os.getenv("MAIN_ADMIN_USER_ID", "").strip()
    if not value.isdigit() or int(value) <= 0:
        return None
    return value


def is_main_admin(username: str | None = None, *, user_id: int | str | None = None) -> bool:
    """Check the main administrator by immutable Telegram user id.

    The username remains an argument for compatibility with older call sites,
    but it is intentionally not an authentication factor.  A missing or
    malformed ``MAIN_ADMIN_USER_ID`` fails closed.
    """

    expected_user_id = configured_main_admin_user_id()
    return expected_user_id is not None and user_id is not None and str(user_id) == expected_user_id


class AccessPolicy:
    """Первый слой доступа Telegram по username-тегам.

    Этот класс оставлен для совместимости с локальными правилами тегов.
    Главный администратор определяется отдельно по неизменяемому Telegram
    user_id и не зависит от username.
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
    role: str = "checker"


class ChatAccessStore:
    """Постоянный список доверенных пользователей по каждому Telegram-чату."""

    def __init__(
        self,
        path: str | Path = "data/access.sqlite3",
        *,
        bootstrap_tags: frozenset[str] = frozenset(),
    ) -> None:
        self.path = str(path)
        self.bootstrap_tags = bootstrap_tags
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
            columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(chat_access)")}
            if "role" not in columns:
                self._connection.execute("ALTER TABLE chat_access ADD COLUMN role TEXT NOT NULL DEFAULT 'checker'")

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def is_allowed(self, chat_id: int | str, *, user_id: int | str | None, username: str | None) -> bool:
        if is_main_admin(username, user_id=user_id):
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

    def role_for(self, chat_id: int | str, *, user_id: int | str | None, username: str | None) -> str | None:
        if is_main_admin(username, user_id=user_id):
            return "owner"
        tag = normalize_tag(username or "") if username else ""
        user_key = str(user_id) if user_id is not None else None
        with self._lock:
            row = self._connection.execute(
                "SELECT role FROM chat_access WHERE chat_id=? AND status='granted' "
                "AND (tag=? OR (user_id IS NOT NULL AND user_id=?)) ORDER BY updated_at DESC LIMIT 1",
                (str(chat_id), tag, user_key),
            ).fetchone()
        return row["role"] if row else ("checker" if tag in self.bootstrap_tags else None)

    def grant(
        self,
        chat_id: int | str,
        *,
        username: str,
        user_id: int | str | None,
        granted_by: int | str,
        role: str = "checker",
    ) -> bool:
        tag = normalize_tag(username)
        if not tag or is_main_admin(user_id=user_id):
            return False
        if role not in {"viewer", "checker", "reviewer", "manager"}:
            return False
        now = self._now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO chat_access (chat_id, tag, user_id, status, updated_at, updated_by, role)
                VALUES (?, ?, ?, 'granted', ?, ?, ?)
                ON CONFLICT(chat_id, tag) DO UPDATE SET
                    user_id = excluded.user_id,
                    status = 'granted',
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by,
                    role = excluded.role
                """,
                (str(chat_id), tag, str(user_id) if user_id is not None else None, now, str(granted_by), role),
            )
        return True

    def revoke(
        self,
        chat_id: int | str,
        *,
        username: str,
        user_id: int | str | None = None,
        revoked_by: int | str,
    ) -> bool:
        tag = normalize_tag(username)
        if not tag or is_main_admin(user_id=user_id):
            return False
        now = self._now()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE chat_access
                SET status = 'revoked', updated_at = ?, updated_by = ?
                WHERE chat_id = ? AND (tag = ? OR (user_id IS NOT NULL AND user_id = ?))
                """,
                (now, str(revoked_by), str(chat_id), tag, str(user_id) if user_id is not None else None),
            )
            if cursor.rowcount:
                return True
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
                SELECT tag, user_id, status, updated_at, role
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
                role=row["role"] or "checker",
            )
            for row in rows
        ]
