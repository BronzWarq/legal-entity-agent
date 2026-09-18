"""Настройки стандартных Telegram-реакций для бота."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Literal, Mapping


# Набор стандартных реакций Telegram Bot API для обычных (не Premium) ботов.
STANDARD_TELEGRAM_REACTIONS: tuple[str, ...] = (
    "❤", "👍", "👎", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢",
    "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳",
    "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓",
    "🍾", "💋", "🖕", "😈", "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈",
    "😇", "😨", "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂", "🤷", "🤷‍♀",
    "😡",
)

ReactionMode = Literal["single", "random"]


@dataclass(frozen=True, slots=True)
class ReactionSettings:
    """Настройки реакции на входящие сообщения."""

    enabled: bool = True
    emojis: tuple[str, ...] = ("👍",)
    mode: ReactionMode = "single"
    custom_emoji_id: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ReactionSettings":
        env = os.environ if environ is None else environ
        enabled = env.get("REACTIONS_ENABLED", "true").strip().casefold() in {
            "1", "true", "yes", "да",
        }
        raw_emojis = env.get("REACTION_EMOJIS")
        if raw_emojis is None:
            # Обратная совместимость с первой настройкой одного эмодзи.
            raw_emojis = env.get("REACTION_EMOJI", "👍")
        raw_emojis = raw_emojis.strip()
        if raw_emojis.casefold() == "all":
            emojis = STANDARD_TELEGRAM_REACTIONS
        else:
            emojis = tuple(part.strip() for part in raw_emojis.split(",") if part.strip())
            if not emojis:
                emojis = ("👍",)
            unsupported = tuple(
                emoji for emoji in emojis if emoji not in STANDARD_TELEGRAM_REACTIONS
            )
            if unsupported:
                raise RuntimeError(
                    "REACTION_EMOJIS содержит неподдерживаемые Telegram-реакции: "
                    + ", ".join(unsupported)
                    + ". Используйте REACTION_EMOJIS=all или эмодзи из официального набора."
                )

        mode = env.get("REACTION_MODE", "single").strip().casefold() or "single"
        if mode not in {"single", "random"}:
            raise RuntimeError("REACTION_MODE должен быть single или random.")
        custom_emoji_id = env.get("REACTION_CUSTOM_EMOJI_ID", "").strip() or None
        return cls(
            enabled=enabled,
            emojis=emojis,
            mode=mode,  # type: ignore[arg-type]
            custom_emoji_id=custom_emoji_id,
        )

    def choose_emoji(self) -> str:
        """Возвращает реакцию согласно режиму single/random."""

        return random.choice(self.emojis) if self.mode == "random" else self.emojis[0]
