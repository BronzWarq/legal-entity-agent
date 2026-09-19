import asyncio

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


@pytest.mark.asyncio
async def test_captcha_pauses_and_resumes_same_query() -> None:
    registry = BatchJobRegistry()
    first, second = _queries("first", "second")
    job = registry.create(chat_id=1, actor_id=10, queries=[first, second], source="history")

    job.pause_for_captcha(first)
    assert job.state is BatchJobState.WAITING_CAPTCHA
    assert job.current_query == first
    waiter = asyncio.create_task(job.wait_for_resume())
    await asyncio.sleep(0)
    assert not waiter.done()

    assert job.resume_after_captcha() is True
    await waiter
    assert job.state is BatchJobState.RUNNING
    assert job.current_query == first


def test_cancel_unblocks_captcha_waiter() -> None:
    registry = BatchJobRegistry()
    query = _queries("first")[0]
    job = registry.create(chat_id=1, actor_id=10, queries=[query], source="history")
    job.pause_for_captcha(query)

    assert job.cancel() is True
    assert job.state is BatchJobState.CANCELLED
    assert job.resume_after_captcha() is False
