from datetime import datetime

import pytest

from legal_entity_agent.agent import LegalEntityAgent
from legal_entity_agent.deep_check import AtomnoFnsCheckAdapter, DeepCheckUnavailable
from legal_entity_agent.models import FnsEntityRecord, InaccuracyState
from legal_entity_agent.render import format_assessment


class FakeFnsClient:
    async def lookup(self, query):
        return FnsEntityRecord(
            query=query,
            source_url="https://egrul.nalog.ru/",
            fetched_at=datetime.now().astimezone(),
            name="ООО Ромашка",
            inn="7707083893",
            inaccuracy_state=InaccuracyState.ABSENT,
        )


class FakeDeepChecker:
    def __init__(self):
        self.identifiers = []

    async def check(self, identifier):
        self.identifiers.append(identifier)
        return {
            "verdict_action": "manual_review_required",
            "verdict_reason_ru": "Источник не ответил полностью.",
            "risks": {
                "overall_risk_level": "unknown",
                "overall_risk_score": 0,
                "coverage": {"total": 7, "answered": 5, "failed": 2},
                "flags": [],
                "errors": [{"message_ru": "КАД недоступен."}],
            },
        }


@pytest.mark.asyncio
async def test_agent_adds_deep_report_without_replacing_fns_result() -> None:
    checker = FakeDeepChecker()
    result = await LegalEntityAgent(FakeFnsClient(), deep_checker=checker).check(
        "ИНН 7707083893"
    )

    assert checker.identifiers == ["7707083893"]
    assert result.record.inaccuracy_state is InaccuracyState.ABSENT
    assert result.deep_report["risks"]["overall_risk_level"] == "unknown"
    rendered = format_assessment(result)
    assert "требуется ручная проверка" in rendered
    assert "Источник не ответил" in rendered


@pytest.mark.asyncio
async def test_agent_does_not_guess_identifier_for_multiple_values() -> None:
    checker = FakeDeepChecker()
    result = await LegalEntityAgent(FakeFnsClient(), deep_checker=checker).check(
        "ИНН 7707083893 ОГРН 1027700132195"
    )

    assert checker.identifiers == []
    assert result.deep_report is None
    assert "ровно один ИНН или ОГРН" in (result.deep_check_error or "")


@pytest.mark.asyncio
async def test_adapter_reports_optional_dependency_without_failing_base_agent(monkeypatch) -> None:
    from legal_entity_agent import deep_check

    def missing(_name):
        raise ModuleNotFoundError("optional package")

    monkeypatch.setattr(deep_check.importlib, "import_module", missing)
    adapter = AtomnoFnsCheckAdapter()
    with pytest.raises(DeepCheckUnavailable):
        await adapter.check("7707083893")
