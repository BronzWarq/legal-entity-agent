from legal_entity_agent.conversation import NaturalIntent, parse_natural_request


def test_natural_check_extracts_labeled_requisites() -> None:
    request = parse_natural_request(
        "Налог, мне нужно проверить компании со следующими данными: ИНН 7707083893 КПП 770401001"
    )

    assert request is not None
    assert request.intent is NaturalIntent.CHECK
    assert request.query_text == "ИНН 7707083893 КПП 770401001"


def test_natural_check_extracts_company_name() -> None:
    request = parse_natural_request("налог, проверь компанию ООО Ромашка, Москва")

    assert request is not None
    assert request.intent is NaturalIntent.CHECK
    assert request.query_text == "ООО Ромашка, Москва"


def test_natural_commands_and_trigger_are_case_insensitive() -> None:
    assert parse_natural_request("НАЛОГ").intent is NaturalIntent.HELP
    assert parse_natural_request("Налог, проверка").intent is NaturalIntent.OPEN_CHECK_APP
    assert parse_natural_request("Налог, проверь все компании").intent is NaturalIntent.CHECK_ALL
    assert parse_natural_request("Налог, покажи историю проверок").intent is NaturalIntent.HISTORY
    assert parse_natural_request("Налог, привет").intent is NaturalIntent.GREETING


def test_natural_skills_request_is_help() -> None:
    assert parse_natural_request("Налог, расскажи о своих навыках").intent is NaturalIntent.HELP


def test_unaddressed_messages_are_ignored() -> None:
    assert parse_natural_request("Проверь компанию с ИНН 7707083893") is None
    assert parse_natural_request("налоговый консультант") is None

