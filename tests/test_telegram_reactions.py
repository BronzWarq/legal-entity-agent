from types import SimpleNamespace

import pytest

from legal_entity_agent import telegram_bot


class FakeBot:
    def __init__(self) -> None:
        self.calls = []

    async def set_message_reaction(self, **kwargs):
        self.calls.append(kwargs)
        return True


def _message(*, is_bot: bool = False):
    return SimpleNamespace(
        chat=SimpleNamespace(id=-100),
        message_id=42,
        from_user=SimpleNamespace(id=7, username="trusted", is_bot=is_bot),
    )


@pytest.mark.asyncio
async def test_reaction_is_added_for_allowed_user(monkeypatch) -> None:
    bot = FakeBot()
    monkeypatch.setattr(telegram_bot, "reactions_enabled", True)
    monkeypatch.setattr(telegram_bot, "reaction_emoji", "✅")
    monkeypatch.setattr(telegram_bot, "_allowed", lambda message: True)

    await telegram_bot._react_to_message(_message(), bot)

    assert bot.calls == [
        {
            "chat_id": -100,
            "message_id": 42,
            "reaction": [
                telegram_bot.ReactionTypeEmoji(emoji="✅"),
            ],
            "is_big": False,
        }
    ]


@pytest.mark.asyncio
async def test_reaction_is_skipped_for_bot_message(monkeypatch) -> None:
    bot = FakeBot()
    monkeypatch.setattr(telegram_bot, "reactions_enabled", True)
    monkeypatch.setattr(telegram_bot, "_allowed", lambda message: True)

    await telegram_bot._react_to_message(_message(is_bot=True), bot)

    assert bot.calls == []


@pytest.mark.asyncio
async def test_reaction_is_skipped_when_disabled(monkeypatch) -> None:
    bot = FakeBot()
    monkeypatch.setattr(telegram_bot, "reactions_enabled", False)
    monkeypatch.setattr(telegram_bot, "_allowed", lambda message: True)

    await telegram_bot._react_to_message(_message(), bot)

    assert bot.calls == []
