import httpx
import pytest

from legal_entity_agent.chat_skill import (
    ChatSkill,
    ChatSkillConfig,
    ChatSkillNotConfigured,
)


@pytest.mark.asyncio
async def test_chat_skill_uses_responses_api_and_keeps_session_context() -> None:
    requests: list[str] = []
    headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers["authorization"])
        requests.append(request.read().decode())
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Ответ по-русски"}],
                    }
                ]
            },
        )

    skill = ChatSkill(
        ChatSkillConfig(api_key="test-key", model="test-model", max_turns=2),
        transport=httpx.MockTransport(handler),
    )

    assert await skill.reply("chat:user", "Привет") == "Ответ по-русски"
    assert await skill.reply("chat:user", "Продолжим") == "Ответ по-русски"
    assert headers[0] == "Bearer test-key"
    assert '"store":false' in requests[0]
    assert "Привет" in requests[1]
    assert "Ответ по-русски" in requests[1]


@pytest.mark.asyncio
async def test_chat_skill_requires_api_key() -> None:
    skill = ChatSkill(ChatSkillConfig(api_key="", model="test-model"))

    with pytest.raises(ChatSkillNotConfigured):
        await skill.reply("chat:user", "Привет")
