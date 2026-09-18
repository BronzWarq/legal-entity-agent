import httpx
import pytest

from legal_entity_agent.fns_client import FnsBlockedError, FnsEgrulClient, FnsNotFoundError


def _client(handler):
    return FnsEgrulClient(
        base_url="https://example.test/",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_lookup_parses_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"t": "token"})
        return httpx.Response(
            200,
            json={
                "rows": [{"name": "ООО Ромашка", "inn": "7707083893"}],
                "kpp": "770401001",
                "message": "Признак недостоверности сведений отсутствует",
            },
        )

    record = await _client(handler).lookup("7707083893")
    assert record.name == "ООО Ромашка"
    assert record.kpp == "770401001"
    assert record.inaccuracy_state.value == "absent"


@pytest.mark.asyncio
async def test_lookup_rejects_captcha() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="CAPTCHA")

    with pytest.raises(FnsBlockedError):
        await _client(handler).lookup("7707083893")


@pytest.mark.asyncio
async def test_lookup_reports_empty_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"t": "token"})
        return httpx.Response(200, json={"rows": []})

    with pytest.raises(FnsNotFoundError):
        await _client(handler).lookup("7707083893")


@pytest.mark.asyncio
async def test_lookup_accepts_free_text_query() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen.append(request.content.decode())
            return httpx.Response(200, json={"t": "token"})
        return httpx.Response(200, json={"name": "ООО Ромашка", "kpp": "770401001"})

    record = await _client(handler).lookup("ООО Ромашка, Москва")
    assert record.name == "ООО Ромашка"
    assert "query=%D0%9E%D0%9E%D0%9E" in seen[0]
