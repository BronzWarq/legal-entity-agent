"""Минимальный журнал административных и проверочных действий."""
from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path


class AuditStore:
    def __init__(self, path: str = "data/audit.sqlite3") -> None:
        self.path = path
        self._lock = threading.RLock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                chat_id TEXT NOT NULL, actor_id TEXT NOT NULL, action TEXT NOT NULL,
                details TEXT NOT NULL)""")

    def record(self, *, chat_id: int | str, actor_id: int | str, action: str, details: str = "") -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO audit_events(created_at,chat_id,actor_id,action,details) VALUES(?,?,?,?,?)",
                             (datetime.now(UTC).isoformat(), str(chat_id), str(actor_id), action, details[:1000]))

    def recent(self, chat_id: int | str, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM audit_events WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                                    (str(chat_id), max(1, min(limit, 100)))).fetchall()

    def close(self) -> None:
        with self._lock:
            self._db.close()
