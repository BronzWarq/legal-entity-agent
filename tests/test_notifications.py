from __future__ import annotations

import pytest

from legal_entity_agent.notifications import (
    EmailSubscriptionStore,
    SmtpConfig,
    normalize_email,
    send_email,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(" User@Example.COM ", "user@example.com"), ("a.b+alerts@example.ru", "a.b+alerts@example.ru")],
)
def test_normalize_email(value: str, expected: str) -> None:
    assert normalize_email(value) == expected


@pytest.mark.parametrize("value", ["", "user", "user@example", "user @example.ru"])
def test_normalize_email_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_email(value)


def test_email_subscription_store_and_alert_state() -> None:
    store = EmailSubscriptionStore(":memory:")
    assert store.get(1) is None
    assert store.set(1, "User@Example.COM", 42) == "user@example.com"
    assert store.get(1) == "user@example.com"
    assert store.alert_state(1, "ИНН 7707083893") is None
    store.mark_alert_state(1, "ИНН 7707083893", "present")
    assert store.alert_state(1, "ИНН 7707083893") == "present"
    assert store.remove(1) is True
    assert store.remove(1) is False
    store.close()


def test_send_email_uses_starttls_and_login(monkeypatch) -> None:
    calls: list[object] = []

    class FakeSmtp:
        def __init__(self, host, port, timeout):
            calls.append((host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def ehlo(self):
            calls.append("ehlo")

        def starttls(self):
            calls.append("starttls")

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            calls.append(message)

    monkeypatch.setattr("legal_entity_agent.notifications.smtplib.SMTP", FakeSmtp)
    send_email(
        SmtpConfig("smtp.example", username="user", password="secret", sender="from@example.com"),
        "to@example.com",
        "Subject",
        "Body",
    )
    assert calls[:4] == [("smtp.example", 587, 20.0), "ehlo", "starttls", "ehlo"]
    assert calls[4] == ("login", "user", "secret")
    assert calls[5]["To"] == "to@example.com"
    assert calls[5].get_content().strip() == "Body"
