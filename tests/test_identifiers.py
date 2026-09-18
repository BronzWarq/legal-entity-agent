import pytest

from legal_entity_agent.identifiers import (
    InvalidIdentifier,
    parse_identifier,
    parse_search_query,
)
from legal_entity_agent.models import IdentifierKind


def test_valid_inn() -> None:
    identifier = parse_identifier("7707 083893")
    assert identifier.value == "7707083893"
    assert identifier.kind is IdentifierKind.INN


def test_valid_ogrn() -> None:
    identifier = parse_identifier("1027700132195")
    assert identifier.kind is IdentifierKind.OGRN


def test_valid_kpp() -> None:
    identifier = parse_identifier("770401001")
    assert identifier.kind is IdentifierKind.KPP


def test_search_query_accepts_multiple_requisites() -> None:
    query = parse_search_query("ИНН 7707083893 КПП 770401001")
    assert query.value == "ИНН 7707083893 КПП 770401001"
    assert {item.kind for item in query.identifiers} == {IdentifierKind.INN, IdentifierKind.KPP}


def test_search_query_accepts_name_and_address() -> None:
    query = parse_search_query("ООО Ромашка, Москва, ул. Тверская, д. 1")
    assert query.identifiers == ()


def test_search_query_accepts_unlabeled_requisites() -> None:
    query = parse_search_query("7707083893 770401001")
    assert [item.kind for item in query.identifiers] == [IdentifierKind.INN, IdentifierKind.KPP]


@pytest.mark.parametrize("value", ["7707083894", "123", "ООО Ромашка", ""])
def test_invalid_identifier(value: str) -> None:
    with pytest.raises(InvalidIdentifier):
        parse_identifier(value)


@pytest.mark.parametrize("value", ["ИНН 123", "ОГРН", "КПП abc"])
def test_labeled_requisite_with_invalid_value_is_not_treated_as_free_text(value: str) -> None:
    with pytest.raises(InvalidIdentifier):
        parse_search_query(value)
