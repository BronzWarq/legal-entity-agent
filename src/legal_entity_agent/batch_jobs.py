"""Состояние пакетных проверок в памяти процесса."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum

from .models import SearchQuery


class BatchJobState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class BatchJob:
    """Одна последовательная очередь массовой проверки."""

    chat_id: int
    actor_id: int
    queries: tuple[SearchQuery, ...]
    source: str
    job_id: str = field(default_factory=lambda: secrets.token_urlsafe(9))
    index: int = 0
    state: BatchJobState = BatchJobState.RUNNING

    @property
    def current_query(self) -> SearchQuery | None:
        if 0 <= self.index < len(self.queries):
            return self.queries[self.index]
        return None

    def cancel(self) -> bool:
        """Отменить выполняющуюся очередь."""

        if self.state in {BatchJobState.COMPLETED, BatchJobState.CANCELLED}:
            return False
        self.state = BatchJobState.CANCELLED
        return True

    def complete(self) -> None:
        self.state = BatchJobState.COMPLETED


class BatchJobRegistry:
    """Реестр активных пакетных заданий с одним заданием на чат."""

    def __init__(self) -> None:
        self._jobs: dict[str, BatchJob] = {}

    def create(self, *, chat_id: int, actor_id: int, queries: list[SearchQuery], source: str) -> BatchJob:
        if self.active_for_chat(chat_id) is not None:
            raise ValueError("В этом чате уже выполняется массовая проверка.")
        job = BatchJob(chat_id=chat_id, actor_id=actor_id, queries=tuple(queries), source=source)
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> BatchJob | None:
        return self._jobs.get(job_id)

    def active_for_chat(self, chat_id: int) -> BatchJob | None:
        for job in self._jobs.values():
            if job.chat_id == chat_id and job.state not in {BatchJobState.COMPLETED, BatchJobState.CANCELLED}:
                return job
        return None

    def remove(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    def __len__(self) -> int:
        return len(self._jobs)
