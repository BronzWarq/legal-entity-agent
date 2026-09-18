from __future__ import annotations

import re

from .models import Identifier, IdentifierKind, SearchQuery


class InvalidIdentifier(ValueError):
    """Реквизит или поисковый запрос нельзя распознать."""


def _digits(value: str) -> str:
    text = value.strip()
    if not text or not re.fullmatch(r"[\d\s()\-]+", text):
        raise InvalidIdentifier("Ожидается ИНН, ОГРН или КПП, состоящий из цифр.")
    return re.sub(r"\D", "", text)


def _checksum(value: str, weights: tuple[int, ...], modulus: int) -> int:
    return sum(int(digit) * weight for digit, weight in zip(value, weights)) % modulus % 10


def _valid_inn(value: str) -> bool:
    if len(value) == 10:
        return _checksum(value[:9], (2, 4, 10, 3, 5, 9, 4, 6, 8), 11) == int(value[-1])
    if len(value) == 12:
        first = _checksum(value[:10], (7, 2, 4, 10, 3, 5, 9, 4, 6, 8), 11)
        second = _checksum(value[:11], (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8, 0), 11)
        return first == int(value[10]) and second == int(value[11])
    return False


def _valid_ogrn(value: str) -> bool:
    if len(value) == 13:
        return int(value[:12]) % 11 % 10 == int(value[-1])
    if len(value) == 15:
        return int(value[:14]) % 13 % 10 == int(value[-1])
    return False


def _valid_kpp(value: str) -> bool:
    return len(value) == 9 and value.isdigit()


def parse_identifier(value: str) -> Identifier:
    digits = _digits(value)
    if len(digits) in (10, 12):
        if not _valid_inn(digits):
            raise InvalidIdentifier("Контрольная сумма ИНН не прошла проверку.")
        return Identifier(digits, IdentifierKind.INN)
    if len(digits) in (13, 15):
        if not _valid_ogrn(digits):
            raise InvalidIdentifier("Контрольная сумма ОГРН не прошла проверку.")
        return Identifier(digits, IdentifierKind.OGRN)
    if len(digits) == 9 and _valid_kpp(digits):
        return Identifier(digits, IdentifierKind.KPP)
    raise InvalidIdentifier("Длина значения не соответствует ИНН, ОГРН или КПП.")


def _labeled_values(value: str, label: str) -> list[str]:
    pattern = rf"(?i)(?:{label})\s*(?:№|N|номер)?\s*[:\-]?\s*(\d{{9,15}})"
    return re.findall(pattern, value)


def parse_search_query(value: str) -> SearchQuery:
    """Принимает один/несколько реквизитов или свободный текст.

    Для цифровых реквизитов контрольные суммы ИНН/ОГРН сохраняются. КПП
    проверяется по длине, поскольку у него нет контрольной суммы. Свободный
    текст передаётся в сервис ФНС без попытки угадать его смысл.
    """

    if not isinstance(value, str) or not value.strip():
        raise InvalidIdentifier("Введите ИНН, ОГРН, КПП или данные юридического лица.")
    normalized = " ".join(value.split())
    identifiers: list[Identifier] = []

    labeled_patterns = (
        ("ИНН", IdentifierKind.INN),
        ("ОГРН", IdentifierKind.OGRN),
        ("ОГРНИП", IdentifierKind.OGRN),
        ("КПП", IdentifierKind.KPP),
    )
    for label, kind in labeled_patterns:
        for digits in _labeled_values(normalized, label):
            if kind is IdentifierKind.INN:
                if len(digits) not in (10, 12) or not _valid_inn(digits):
                    raise InvalidIdentifier(f"Контрольная сумма {kind.value} не прошла проверку.")
            elif kind is IdentifierKind.OGRN:
                if len(digits) not in (13, 15) or not _valid_ogrn(digits):
                    raise InvalidIdentifier(f"Контрольная сумма {kind.value} не прошла проверку.")
            elif not _valid_kpp(digits):
                raise InvalidIdentifier("КПП должен состоять из 9 цифр.")
            item = Identifier(digits, kind)
            if item not in identifiers:
                identifiers.append(item)

    # Допускаем несколько реквизитов без подписей, например: ИНН КПП.
    if not identifiers:
        candidates = re.findall(r"(?<!\d)\d{9,15}(?!\d)", normalized)
        for digits in candidates:
            item = parse_identifier(digits)
            if item not in identifiers:
                identifiers.append(item)

    # Для одного чисто цифрового значения сохраняем строгую старую семантику.
    if not identifiers and re.fullmatch(r"[\d\s()\-]+", normalized):
        digits = re.sub(r"\D", "", normalized)
        if len(digits) == 9:
            identifiers.append(Identifier(digits, IdentifierKind.KPP))
        else:
            identifiers.append(parse_identifier(digits))

    return SearchQuery(normalized, tuple(identifiers))
