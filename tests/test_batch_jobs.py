import pytest

from legal_entity_agent.batch_jobs import BatchJobRegistry, BatchJobState
from legal_entity_agent.models import SearchQuery


def _queries(*values: str) -> list[SearchQuery]:
    return [SearchQuery(value) for value in values]


def test_registry_allows_only_one_active_job_per_chat() -> None:
    registry = BatchJobRegistry()
    job = registry.create(chat_id=1, actor_id=10, queries=_queries("a"), source="history")

    with pytest.raises(ValueError, match="уже выполняется"):
        registry.create(chat_id=1, actor_id=11, queries=_queries("b"), source="upload")

    job.complete()
    second = registry.create(chat_id=1, actor_id=11, queries=_queries("b"), source="upload")
    assert second.job_id != job.job_id


def test_cancel_stops_active_job() -> None:
    registry = BatchJobRegistry()
    job = registry.create(chat_id=1, actor_id=10, queries=_queries("first"), source="history")

    assert job.cancel() is True
    assert job.state is BatchJobState.CANCELLED
    assert job.cancel() is False
