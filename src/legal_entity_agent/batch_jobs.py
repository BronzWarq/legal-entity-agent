"""Состояние пакетных проверок и ручного подтверждения CAPTCHA.

Модуль не обращается к сети и не пытается решать CAPTCHA. Он хранит только
состояние задания в памяти процесса: это позволяет приостановить ровно одну
очередь и возобновить её после нажатия пользователем кнопки подтверждения.
Сырые ответы ФНС и данные CAPTCHA сюда не записываются.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from enum import StrEnum

from .models import SearchQuery


class BatchJobState(StrEnum):
    RUNNING = "running"
    WAITING_CAPTCHA = "waiting_captcha"
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
    captcha_query: SearchQuery | None = None
    _resume_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def current_query(self) -> SearchQuery | None:
        if 0 <= self.index < len(self.queries):
            return self.queries[self.index]
        return None

    def pause_for_captcha(self, query: SearchQuery) -> None:
        """Перевести задание в ожидание ручного прохождения CAPTCHA."""

        self.captcha_query = query
        self.state = BatchJobState.WAITING_CAPTCHA
        self._resume_event.clear()

    async def wait_for_resume(self) -> None:
        """Ожидать подтверждения пользователя без блокировки polling-цикла."""

        await self._resume_event.wait()

    def resume_after_captcha(self) -> bool:
        """Разрешить повтор текущего запроса; вернуть False для другой стадии."""

        if self.state is not BatchJobState.WAITING_CAPTCHA:
            return False
        self.captcha_query = None
        self.state = BatchJobState.RUNNING
        self._resume_event.set()
        return True

    def cancel(self) -> bool:
        """Отменить ожидающую или выполняющуюся очередь."""

        if self.state in {BatchJobState.COMPLETED, BatchJobState.CANCELLED}:
            return False
        self.state = BatchJobState.CANCELLED
        self._resume_event.set()
        return True

    def complete(self) -> None:
        self.state = BatchJobState.COMPLETED
        self.captcha_query = None
        self._resume_event.set()


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
