"""Необязательный разговорный skill через OpenAI Responses API.

Skill не использует cookies, пароль или веб-сессию ChatGPT. Для внешнего
приложения применяется отдельный API-ключ, который не хранится в репозитории.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx


class ChatSkillError(RuntimeError):
    """Ошибка обращения к разговорному провайдеру."""


class ChatSkillNotConfigured(ChatSkillError):
    """Разговорный провайдер не включён, потому что не задан API-ключ."""


@dataclass(frozen=True, slots=True)
class ChatSkillConfig:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout: float = 30.0
    max_turns: int = 8
    system_prompt: str = (
        "Ты — разговорный модуль агента «Налог» для Telegram. "
        "Отвечай по-русски, кратко и понятно. "
        "Не выдумывай результаты проверок ФНС, если они не переданы отдельным "
        "инструментом. Не проси и не сохраняй пароли, API-ключи и cookies. "
        "Если пользователь хочет проверить юридическое лицо, предложи указать "
        "ИНН, ОГРН, КПП, название или адрес."
    )


class ChatSkill:
    """Ведёт отдельный короткий диалог для каждого Telegram-чата и пользователя."""

    def __init__(
        self,
        config: ChatSkillConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self._history: dict[str, list[tuple[str, str]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def from_env(cls) -> "ChatSkill | None":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        model = os.getenv("OPENAI_MODEL", "").strip()
        if not model:
            raise RuntimeError("Для ChatSkill нужно задать OPENAI_MODEL в .env")
        try:
            timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30"))
            max_turns = int(os.getenv("CHAT_HISTORY_TURNS", "8"))
        except ValueError as exc:
            raise RuntimeError(
                "OPENAI_TIMEOUT_SECONDS и CHAT_HISTORY_TURNS должны быть числовыми значениями."
            ) from exc
        if timeout <= 0 or max_turns < 1:
            raise RuntimeError("OPENAI_TIMEOUT_SECONDS должен быть положительным, CHAT_HISTORY_TURNS — не менее 1.")
        return cls(
            ChatSkillConfig(
                api_key=api_key,
                model=model,
                base_url=os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
                or "https://api.openai.com/v1",
                timeout=timeout,
                max_turns=max_turns,
            )
        )

    @property
    def configured(self) -> bool:
        return bool(self.config.api_key)

    def clear(self, session_id: str) -> None:
        self._history.pop(session_id, None)
        self._locks.pop(session_id, None)

    async def reply(self, session_id: str, user_text: str) -> str:
        if not self.configured:
            raise ChatSkillNotConfigured("OPENAI_API_KEY не задан.")
        text = " ".join(user_text.split())
        if not text:
            raise ChatSkillError("Пустое сообщение нельзя передать в ChatSkill.")

        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            history = self._history.setdefault(session_id, [])
            messages: list[dict[str, str]] = [
                {"role": "system", "content": self.config.system_prompt}
            ]
            for role, content in history[-self.config.max_turns * 2 :]:
                messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": text})

            payload = {
                "model": self.config.model,
                "input": messages,
                "store": False,
            }
            headers = {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            }
            base_url = self.config.base_url.strip().rstrip("/") or "https://api.openai.com/v1"
            async with httpx.AsyncClient(
                base_url=base_url + "/",
                headers=headers,
                timeout=self.config.timeout,
                transport=self.transport,
            ) as client:
                try:
                    response = await client.post("responses", json=payload)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise ChatSkillError(
                        f"Не удалось получить ответ разговорного провайдера: {exc}"
                    ) from exc

            try:
                data = response.json()
            except ValueError as exc:
                raise ChatSkillError("Разговорный провайдер вернул некорректный JSON.") from exc
            answer = self._extract_text(data)
            if not answer:
                raise ChatSkillError("Разговорный провайдер не вернул текстовый ответ.")
            history.extend((("user", text), ("assistant", answer)))
            self._history[session_id] = history[-self.config.max_turns * 2 :]
            return answer

    @staticmethod
    def _extract_text(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()
        chunks: list[str] = []
        for item in payload.get("output", []):
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []):
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())
        return "\n".join(chunks)
