from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class IdentifierKind(str, Enum):
    INN = "ИНН"
    OGRN = "ОГРН"
    KPP = "КПП"


class InaccuracyState(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    NOT_REPORTED = "not_reported"


@dataclass(frozen=True, slots=True)
class Identifier:
    value: str
    kind: IdentifierKind


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Нормализованный запрос пользователя к сервису ФНС.

    Запрос может содержать один реквизит, несколько реквизитов или свободный
    текст: например, название и адрес юридического лица.
    """

    value: str
    identifiers: tuple[Identifier, ...] = ()


@dataclass(slots=True)
class FnsEntityRecord:
    query: SearchQuery
    source_url: str
    fetched_at: datetime
    name: str | None = None
    inn: str | None = None
    ogrn: str | None = None
    kpp: str | None = None
    status: str | None = None
    registration_date: date | None = None
    address: str | None = None
    inaccuracy_state: InaccuracyState = InaccuracyState.NOT_REPORTED
    inaccuracy_markers: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class Assessment:
    record: FnsEntityRecord
    # Необязательный структурированный отчёт расширенной проверки
    # atomno-mcp-fns-check. Он не заменяет ответ ЕГРЮЛ и не используется для
    # вывода «недостоверность отсутствует».
    deep_report: dict[str, Any] | None = None
    deep_check_error: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.record.name or self.record.inn or self.record.ogrn or self.record.kpp)

    @property
    def has_inaccurate_information(self) -> bool | None:
        if self.record.inaccuracy_state is InaccuracyState.PRESENT:
            return True
        if self.record.inaccuracy_state is InaccuracyState.ABSENT:
            return False
        return None
