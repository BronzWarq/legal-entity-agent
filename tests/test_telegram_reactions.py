from types import SimpleNamespace

import pytest

from legal_entity_agent import telegram_bot
from legal_entity_agent.reactions import STANDARD_TELEGRAM_REACTIONS, ReactionSettings


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
    monkeypatch.setattr(telegram_bot, "reaction_settings", ReactionSettings(emojis=("🔥",)))
    monkeypatch.setattr(telegram_bot, "_allowed", lambda message: True)

    await telegram_bot._react_to_message(_message(), bot)

    assert bot.calls == [
        {
            "chat_id": -100,
            "message_id": 42,
            "reaction": [
                telegram_bot.ReactionTypeEmoji(emoji="🔥"),
            ],
            "is_big": False,
        }
    ]


@pytest.mark.asyncio
async def test_reaction_is_skipped_for_bot_message(monkeypatch) -> None:
    bot = FakeBot()
    monkeypatch.setattr(telegram_bot, "reaction_settings", ReactionSettings())
    monkeypatch.setattr(telegram_bot, "_allowed", lambda message: True)

    await telegram_bot._react_to_message(_message(is_bot=True), bot)

    assert bot.calls == []


@pytest.mark.asyncio
async def test_reaction_is_skipped_when_disabled(monkeypatch) -> None:
    bot = FakeBot()
    monkeypatch.setattr(telegram_bot, "reaction_settings", ReactionSettings(enabled=False))
    monkeypatch.setattr(telegram_bot, "_allowed", lambda message: True)

    await telegram_bot._react_to_message(_message(), bot)

    assert bot.calls == []


@pytest.mark.asyncio
async def test_custom_emoji_reaction_is_sent_as_custom_type(monkeypatch) -> None:
    bot = FakeBot()
    monkeypatch.setattr(
        telegram_bot,
        "reaction_settings",
        ReactionSettings(custom_emoji_id="123456789"),
    )
    monkeypatch.setattr(telegram_bot, "_allowed", lambda message: True)

    await telegram_bot._react_to_message(_message(), bot)

    assert bot.calls[0]["reaction"][0].custom_emoji_id == "123456789"


def test_all_reaction_settings_include_full_standard_set() -> None:
    settings = ReactionSettings.from_env({"REACTION_EMOJIS": "all", "REACTION_MODE": "random"})

    assert settings.emojis == STANDARD_TELEGRAM_REACTIONS
    assert settings.mode == "random"


def test_custom_emoji_reaction_settings_are_supported() -> None:
    settings = ReactionSettings.from_env({"REACTION_CUSTOM_EMOJI_ID": "123456789"})

    assert settings.custom_emoji_id == "123456789"
