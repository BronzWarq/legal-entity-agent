"""Реестр лицензий и сроки их действия.

Модуль не подменяет официальный реестр лицензий: он хранит подтверждённые
оператором сведения и рассчитывает срок действия. Источник и дата проверки
сохраняются для аудита, а отсутствие записи не трактуется как отсутствие
лицензии.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LicenseRecord:
    license_id: str
    inn: str
    name: str | None
    license_type: str
    issued_at: date | None
    expires_at: date | None
    region: str | None
    source_url: str | None
    checked_at: datetime
    notes: str | None

    def state(self, today: date | None = None) -> str:
        today = today or date.today()
        if self.expires_at is None:
            return "срок не указан"
        if self.expires_at < today:
            return "истекла"
        return "действует"

    def days_left(self, today: date | None = None) -> int | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - (today or date.today())).days


class LicenseStore:
    """SQLite-хранилище лицензий с безопасным upsert по номеру лицензии."""

    def __init__(self, path: str | Path = "data/licenses.sqlite3") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS licenses (
                    license_id TEXT PRIMARY KEY,
                    inn TEXT NOT NULL,
                    name TEXT,
                    license_type TEXT NOT NULL,
                    issued_at TEXT,
                    expires_at TEXT,
                    region TEXT,
                    source_url TEXT,
                    checked_at TEXT NOT NULL,
                    notes TEXT
                )"""
            )
            self._connection.execute("CREATE INDEX IF NOT EXISTS idx_licenses_inn ON licenses (inn)")

    def upsert(self, record: LicenseRecord) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO licenses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(license_id) DO UPDATE SET
                  inn=excluded.inn, name=excluded.name, license_type=excluded.license_type,
                  issued_at=excluded.issued_at, expires_at=excluded.expires_at,
                  region=excluded.region, source_url=excluded.source_url,
                  checked_at=excluded.checked_at, notes=excluded.notes""",
                (record.license_id, record.inn, record.name, record.license_type,
                 record.issued_at.isoformat() if record.issued_at else None,
                 record.expires_at.isoformat() if record.expires_at else None,
                 record.region, record.source_url, record.checked_at.isoformat(), record.notes),
            )

    def for_inn(self, inn: str) -> list[LicenseRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM licenses WHERE inn = ? ORDER BY expires_at", (inn,)
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def expiring(self, within_days: int = 30) -> list[LicenseRecord]:
        today = date.today()
        deadline = today.fromordinal(today.toordinal() + max(0, within_days))
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM licenses WHERE expires_at IS NOT NULL AND expires_at BETWEEN ? AND ? ORDER BY expires_at",
                (today.isoformat(), deadline.isoformat()),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def all(self) -> list[LicenseRecord]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM licenses ORDER BY expires_at").fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> LicenseRecord:
        return LicenseRecord(
            license_id=row["license_id"], inn=row["inn"], name=row["name"],
            license_type=row["license_type"],
            issued_at=date.fromisoformat(row["issued_at"]) if row["issued_at"] else None,
            expires_at=date.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            region=row["region"], source_url=row["source_url"],
            checked_at=datetime.fromisoformat(row["checked_at"]), notes=row["notes"],
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def new_license(*, license_id: str, inn: str, license_type: str = "алкогольная",
                expires_at: date | None = None, name: str | None = None,
                source_url: str | None = None, region: str | None = None,
                notes: str | None = None) -> LicenseRecord:
    return LicenseRecord(license_id=license_id, inn=inn, name=name,
        license_type=license_type, issued_at=None, expires_at=expires_at,
        region=region, source_url=source_url,
        checked_at=datetime.now(UTC), notes=notes)
