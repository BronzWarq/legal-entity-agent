"""Список компаний для последующего регулярного мониторинга."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path


class WatchStore:
    def __init__(self, path: str = "data/watchlist.sqlite3") -> None:
        self._lock = threading.RLock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS watchlist(
                chat_id TEXT NOT NULL, query TEXT NOT NULL, added_by TEXT NOT NULL,
                added_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                last_digest TEXT, last_checked_at TEXT, last_inaccuracy_state TEXT,
                PRIMARY KEY(chat_id, query))""")
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(watchlist)")}
            if "last_digest" not in columns:
                self._db.execute("ALTER TABLE watchlist ADD COLUMN last_digest TEXT")
            if "last_checked_at" not in columns:
                self._db.execute("ALTER TABLE watchlist ADD COLUMN last_checked_at TEXT")
            if "last_inaccuracy_state" not in columns:
                self._db.execute("ALTER TABLE watchlist ADD COLUMN last_inaccuracy_state TEXT")

    def add(self, chat_id: int | str, query: str, actor_id: int | str) -> None:
        with self._lock, self._db:
            self._db.execute("""INSERT INTO watchlist(chat_id, query, added_by, added_at, active)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(chat_id, query) DO UPDATE SET active=1, added_by=excluded.added_by,
                added_at=excluded.added_at""",
                (str(chat_id), query.strip(), str(actor_id), datetime.now(UTC).isoformat()))

    def remove(self, chat_id: int | str, query: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE watchlist SET active=0 WHERE chat_id=? AND query=? AND active=1",
                                     (str(chat_id), query.strip()))
            return bool(cursor.rowcount)

    def list(self, chat_id: int | str) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT query FROM watchlist WHERE chat_id=? AND active=1 ORDER BY added_at",
                                    (str(chat_id),)).fetchall()
        return [row[0] for row in rows]

    def entries(self) -> list[tuple[str, str, str | None]]:
        with self._lock:
            return self._db.execute(
                "SELECT chat_id, query, last_digest FROM watchlist WHERE active=1 ORDER BY chat_id, added_at"
            ).fetchall()

    def update_snapshot(self, chat_id: int | str, query: str, snapshot: str) -> bool:
        return self.update_snapshot_with_state(chat_id, query, snapshot)

    def update_snapshot_with_state(
        self,
        chat_id: int | str,
        query: str,
        snapshot: str,
        *,
        inaccuracy_state: str | None = None,
    ) -> bool:
        digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        with self._lock, self._db:
            row = self._db.execute("SELECT last_digest FROM watchlist WHERE chat_id=? AND query=? AND active=1",
                                   (str(chat_id), query)).fetchone()
            if row is None:
                return False
            changed = row[0] is not None and row[0] != digest
            self._db.execute(
                """UPDATE watchlist SET last_digest=?, last_checked_at=?,
                last_inaccuracy_state=COALESCE(?, last_inaccuracy_state)
                WHERE chat_id=? AND query=?""",
                (digest, datetime.now(UTC).isoformat(), inaccuracy_state, str(chat_id), query),
            )
            return changed

    def snapshot_state(self, chat_id: int | str, query: str) -> tuple[str | None, str | None]:
        """Return the previous digest and inaccuracy state for a watched query."""

        with self._lock:
            row = self._db.execute(
                "SELECT last_digest, last_inaccuracy_state FROM watchlist "
                "WHERE chat_id=? AND query=? AND active=1",
                (str(chat_id), query),
            ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def close(self) -> None:
        with self._lock:
            self._db.close()
