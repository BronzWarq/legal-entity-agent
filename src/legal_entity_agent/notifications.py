"""Email subscriptions and SMTP delivery for monitoring alerts."""
from __future__ import annotations

import re
import smtplib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FIXED_NOTIFICATION_EMAIL = "jurist@wrf.su"


def normalize_email(value: str) -> str:
    """Validate and normalize a user-provided email address."""

    email = value.strip().casefold()
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        raise ValueError("Укажите корректный адрес электронной почты.")
    return email


class EmailSubscriptionStore:
    """Stores one notification address for each Telegram chat."""

    def __init__(self, path: str = "data/notifications.sqlite3") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS email_subscriptions(
                chat_id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS email_alerts(
                chat_id TEXT NOT NULL,
                query TEXT NOT NULL,
                state TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, query)
            )"""
        )
        self._db.commit()

    def set(self, chat_id: int | str, email: str, actor_id: int | str) -> str:
        normalized = normalize_email(email)
        with self._db:
            self._db.execute(
                """INSERT INTO email_subscriptions(chat_id, email, updated_by, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET email=excluded.email,
                    updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                (str(chat_id), normalized, str(actor_id), datetime.now(UTC).isoformat()),
            )
        return normalized

    def get(self, chat_id: int | str) -> str | None:
        row = self._db.execute(
            "SELECT email FROM email_subscriptions WHERE chat_id=?", (str(chat_id),)
        ).fetchone()
        return row[0] if row else None

    def remove(self, chat_id: int | str) -> bool:
        with self._db:
            cursor = self._db.execute(
                "DELETE FROM email_subscriptions WHERE chat_id=?", (str(chat_id),)
            )
        return bool(cursor.rowcount)

    def alert_state(self, chat_id: int | str, query: str) -> str | None:
        row = self._db.execute(
            "SELECT state FROM email_alerts WHERE chat_id=? AND query=?",
            (str(chat_id), query),
        ).fetchone()
        return row[0] if row else None

    def mark_alert_state(self, chat_id: int | str, query: str, state: str) -> None:
        with self._db:
            self._db.execute(
                """INSERT INTO email_alerts(chat_id, query, state, sent_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, query) DO UPDATE SET state=excluded.state,
                    sent_at=excluded.sent_at""",
                (str(chat_id), query, state, datetime.now(UTC).isoformat()),
            )

    def close(self) -> None:
        self._db.close()


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    host: str
    port: int = 587
    username: str = ""
    password: str = ""
    sender: str = ""
    starttls: bool = True
    timeout: float = 20.0

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)


def send_email(config: SmtpConfig, recipient: str, subject: str, body: str) -> None:
    """Send one plain-text message through the configured SMTP relay."""

    if not config.configured:
        raise RuntimeError("SMTP не настроен: задайте SMTP_HOST и SMTP_FROM.")
    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = normalize_email(recipient)
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(config.host, config.port, timeout=config.timeout) as smtp:
        smtp.ehlo()
        if config.starttls:
            smtp.starttls()
            smtp.ehlo()
        if config.username:
            smtp.login(config.username, config.password)
        smtp.send_message(message)
