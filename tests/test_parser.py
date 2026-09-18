from legal_entity_agent.models import Identifier, IdentifierKind, InaccuracyState
from legal_entity_agent.parser import parse_fns_payload


def test_parser_detects_absent_inaccuracy() -> None:
    record = parse_fns_payload(
        {
            "rows": [{"name": "ООО Ромашка", "inn": "7707083893"}],
            "status": "Действующее",
            "message": "Признак недостоверности сведений отсутствует",
        },
        Identifier("7707083893", IdentifierKind.INN),
        "https://egrul.nalog.ru/search-result/test",
    )
    assert record.inaccuracy_state is InaccuracyState.ABSENT
    assert record.name == "ООО Ромашка"


def test_parser_detects_present_inaccuracy() -> None:
    record = parse_fns_payload(
        {
            "name": "ООО Ромашка",
            "inn": "7707083893",
            "status": "В ЕГРЮЛ внесена запись о недостоверности сведений об адресе",
        },
        Identifier("7707083893", IdentifierKind.INN),
        "https://egrul.nalog.ru/search-result/test",
    )
    assert record.inaccuracy_state is InaccuracyState.PRESENT


def test_parser_does_not_treat_unqualified_inaccuracy_word_as_positive() -> None:
    record = parse_fns_payload(
        {"name": "ООО Ромашка", "message": "Проверка недостоверности доступна"},
        Identifier("7707083893", IdentifierKind.INN),
        "https://egrul.nalog.ru/search-result/test",
    )
    assert record.inaccuracy_state is InaccuracyState.NOT_REPORTED


def test_parser_accepts_structured_inaccuracy_flag() -> None:
    record = parse_fns_payload(
        {"name": "ООО Ромашка", "has_inaccuracy": True},
        Identifier("7707083893", IdentifierKind.INN),
        "https://egrul.nalog.ru/search-result/test",
    )
    assert record.inaccuracy_state is InaccuracyState.PRESENT
