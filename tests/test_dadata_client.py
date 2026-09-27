from datetime import datetime

import httpx
import pytest

from legal_entity_agent.dadata_client import DadataClient
from legal_entity_agent.fns_client import FnsNotFoundError, FnsTransientError
from legal_entity_agent.models import InaccuracyState


def _client(handler):
    return DadataClient(
        "test-token",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_lookup_parses_company_and_inaccuracy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Token test-token"
        assert request.url.path.endswith("/findById/party")
        return httpx.Response(
            200,
            json={
                "suggestions": [
                    {
                        "value": "ООО Ромашка",
                        "data": {
                            "inn": "7707083893",
                            "ogrn": "1027700132195",
                            "kpp": "770401001",
                            "name": {"full_with_opf": "ООО Ромашка"},
                            "state": {
                                "status": "ACTIVE",
                                "registration_date": "2002-01-01",
                            },
                            "address": {"value": "г Москва"},
                            "invalid": True,
                            "address": {"value": "г Москва", "invalidity": True},
                        },
                    }
                ]
            },
        )

    record = await _client(handler).lookup("7707083893")
    assert record.name == "ООО Ромашка"
    assert record.inn == "7707083893"
    assert record.ogrn == "1027700132195"
    assert record.status == "ACTIVE"
    assert record.inaccuracy_state is InaccuracyState.PRESENT
    assert record.inaccuracy_markers == ("Недостоверные сведения об адресе.",)
    assert record.raw["provider"] == "dadata"
    assert record.fetched_at <= datetime.now(record.fetched_at.tzinfo)


@pytest.mark.asyncio
async def test_lookup_reports_absent_inaccuracy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/findById/party")
        return httpx.Response(
            200,
            json={
                "suggestions": [
                    {
                        "data": {
                            "inn": "7707083893",
                            "name": {"full_with_opf": "ООО Ромашка"},
                            "invalid": None,
                        }
                    }
                ]
            },
        )

    record = await _client(handler).lookup("7707083893")
    assert record.inaccuracy_state is InaccuracyState.ABSENT


@pytest.mark.asyncio
async def test_lookup_uses_suggest_for_free_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/suggest/party")
        return httpx.Response(
            200,
            json={
                "suggestions": [
                    {
                        "value": "ООО Ромашка",
                        "data": {
                            "name": {"full_with_opf": "ООО Ромашка"},
                            "invalid": None,
                        },
                    }
                ]
            },
        )

    record = await _client(handler).lookup("ООО Ромашка")
    assert record.name == "ООО Ромашка"


@pytest.mark.asyncio
async def test_lookup_reports_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"suggestions": []})

    with pytest.raises(FnsNotFoundError):
        await _client(handler).lookup("7707083893")


@pytest.mark.asyncio
async def test_lookup_reports_transient_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "rate limit"})

    with pytest.raises(FnsTransientError, match="временно"):
        await _client(handler).lookup("7707083893")
