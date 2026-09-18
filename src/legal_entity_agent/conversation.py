"""Распознавание русскоязычных обращений к боту «Налог»."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class NaturalIntent(StrEnum):
    HELP = "help"
    GREETING = "greeting"
    CHECK = "check"
    CHECK_ALL = "check_all"
    HISTORY = "history"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class NaturalRequest:
    intent: NaturalIntent
    query_text: str = ""


_TRIGGER_RE = re.compile(r"(?is)^\s*налог\b[\s,!:;—–-]*(.*)$")
_IDENTIFIER_RE = re.compile(r"(?i)\b(?:ИНН|ОГРН|ОГРНИП|КПП)\b")
_NUMBER_RE = re.compile(r"(?<!\d)\d{9,15}(?!\d)")
_CHECK_WORDS = (
    "проверь",
    "проверить",
    "проверка",
    "проверки",
    "найди",
    "найти",
    "узнай",
    "узнать",
    "сверь",
    "поищи",
    "обнови",
    "обновить",
)


def _clean_query(body: str) -> str:
    """Убирает вежливую часть фразы, оставляя данные для поиска ФНС."""

    labeled = _IDENTIFIER_RE.search(body)
    if labeled:
        return body[labeled.start() :].strip(" \t\n:;,.—–-")

    numbered = _NUMBER_RE.search(body)
    if numbered:
        return body[numbered.start() :].strip(" \t\n:;,.—–-")

    query = body.strip()
    query = re.sub(
        r"(?iu)^\s*(?:мне\s+нужно\s+|мне\s+надо\s+|пожалуйста\s+|можешь\s+|нужно\s+|надо\s+)?"
        r"(?:проверь(?:те)?|проверить|найди(?:те)?|узнай(?:те)?|сверь(?:те)?|поищи(?:те)?)"
        r"\s*[,;:—–-]?\s*",
        "",
        query,
    )
    query = re.sub(
        r"(?iu)^\s*(?:компани(?:ю|и)|юрлицо|юридическое\s+лицо|организацию)"
        r"\s*(?:с\s+|по\s+)?",
        "",
        query,
    )
    query = re.sub(
        r"(?iu)^\s*(?:со\s+следующими\s+данными|по\s+данным)\s*[:—–-]?\s*",
        "",
        query,
    )
    return query.strip(" \t\n:;,.—–-")


def parse_natural_request(text: str) -> NaturalRequest | None:
    """Распознаёт обращение, начинающееся с имени «Налог».

    Функция намеренно не делает сетевых запросов и не принимает решений о
    доступе. Она лишь определяет намерение и извлекает текст проверки.
    Контрольные суммы и остальные проверки выполняются существующим парсером
    реквизитов перед обращением к ФНС.
    """

    if not isinstance(text, str):
        return None
    match = _TRIGGER_RE.match(text)
    if not match:
        return None
    body = " ".join(match.group(1).split())
    lowered = body.casefold()
    if not body:
        return NaturalRequest(NaturalIntent.HELP)
    if any(marker in lowered for marker in ("помоги", "что ты умеешь", "справк", "команд")):
        return NaturalRequest(NaturalIntent.HELP)
    if re.match(
        r"(?iu)^(?:привет|здравствуй|добрый\s+день|доброе\s+утро|добрый\s+вечер)\b",
        body,
    ):
        return NaturalRequest(NaturalIntent.GREETING)

    has_check_word = any(word in lowered for word in _CHECK_WORDS)
    if has_check_word and re.search(r"(?iu)\bвсе\b", body):
        return NaturalRequest(NaturalIntent.CHECK_ALL)
    if any(marker in lowered for marker in ("истори", "ранее провер", "предыдущ")):
        return NaturalRequest(NaturalIntent.HISTORY)

    query_text = _clean_query(body)
    has_identifier = bool(_IDENTIFIER_RE.search(body) or _NUMBER_RE.search(body))
    if has_check_word or has_identifier:
        return NaturalRequest(NaturalIntent.CHECK, query_text)
    return NaturalRequest(NaturalIntent.UNKNOWN, body)
