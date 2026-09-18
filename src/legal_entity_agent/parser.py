from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from .models import FnsEntityRecord, Identifier, InaccuracyState, SearchQuery


_INACCURACY_WORDS = ("недостовер", "признак недостоверности", "недостоверность")
_ABSENT_PHRASES = (
    "недостоверные сведения отсутствуют",
    "недостоверность сведений отсутствует",
    "признак недостоверности отсутствует",
    "признак недостоверности сведений отсутствует",
    "сведения о недостоверности отсутствуют",
    "отсутствуют недостоверные сведения",
    "недостоверных сведений не выявлено",
    "признаки недостоверности не выявлены",
    "нет недостоверных сведений",
)
_PRESENT_PHRASES = (
    "внесена запись о недостоверности",
    "есть сведения о недостоверности",
    "выявлены недостоверные сведения",
    "признак недостоверности установлен",
    "сведения недостоверны",
)
_INACCURACY_KEYS = {
    "inaccuracy",
    "isinaccurate",
    "hasinaccuracy",
    "invalidinformation",
    "hasinvalidinformation",
    "недостоверность",
    "признакнедостоверности",
}


def _walk(value: Any) -> Iterable[tuple[str | None, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield None, item
            yield from _walk(item)


def _first(payload: Any, names: tuple[str, ...]) -> Any:
    wanted = {name.casefold() for name in names}
    for key, value in _walk(payload):
        if key and key.casefold() in wanted and isinstance(value, (str, int, float)):
            return str(value)
    return None


def _strings(payload: Any) -> Iterable[str]:
    for _, value in _walk(payload):
        if isinstance(value, str):
            yield value.strip()


def _markers(payload: Any) -> tuple[str, ...]:
    found: list[str] = []
    for value in _strings(payload):
        lower = value.casefold()
        if any(word in lower for word in _INACCURACY_WORDS):
            if value not in found:
                found.append(value)
    return tuple(found)


def _inaccuracy_state(payload: Any, markers: tuple[str, ...]) -> InaccuracyState:
    structured_state: InaccuracyState | None = None
    for key, value in _walk(payload):
        if key and key.casefold().replace("_", "") in _INACCURACY_KEYS and isinstance(value, bool):
            state = InaccuracyState.PRESENT if value else InaccuracyState.ABSENT
            if structured_state is not None and structured_state is not state:
                return InaccuracyState.NOT_REPORTED
            structured_state = state
    lowered_markers = tuple(value.casefold() for value in markers)
    has_present_text = any(
        any(phrase in value for phrase in _PRESENT_PHRASES) for value in lowered_markers
    )
    has_absent_text = any(
        any(phrase in value for phrase in _ABSENT_PHRASES) for value in lowered_markers
    )
    if has_present_text and has_absent_text:
        return InaccuracyState.NOT_REPORTED
    if has_present_text:
        text_state = InaccuracyState.PRESENT
    elif has_absent_text:
        text_state = InaccuracyState.ABSENT
    else:
        text_state = None
    if structured_state is not None and text_state is not None and structured_state is not text_state:
        return InaccuracyState.NOT_REPORTED
    return text_state or structured_state or InaccuracyState.NOT_REPORTED


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"(\d{2})[./-](\d{2})[./-](\d{4})", value)
    if not match:
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                return None
        return None
    try:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    except ValueError:
        return None


def parse_fns_payload(
    payload: dict[str, Any], query: SearchQuery | Identifier, source_url: str
) -> FnsEntityRecord:
    if isinstance(query, Identifier):
        query = SearchQuery(query.value, (query,))
    markers = _markers(payload)
    return FnsEntityRecord(
        query=query,
        source_url=source_url,
        fetched_at=datetime.now().astimezone(),
        name=_first(payload, ("name", "fullName", "shortName", "Наименование")),
        inn=_first(payload, ("inn", "INN", "ИНН")),
        ogrn=_first(payload, ("ogrn", "OGRN", "ОГРН")),
        kpp=_first(payload, ("kpp", "KPP", "КПП")),
        status=_first(payload, ("status", "Статус", "recordStatus")),
        registration_date=_as_date(_first(payload, ("regDate", "registerDate", "registrationDate"))),
        address=_first(payload, ("address", "Address", "Адрес")),
        inaccuracy_state=_inaccuracy_state(payload, markers),
        inaccuracy_markers=markers,
        raw=payload,
    )
