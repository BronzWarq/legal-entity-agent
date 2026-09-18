from legal_entity_agent.bulk_import import parse_upload


def test_csv_import_preserves_requisite_labels() -> None:
    rows = parse_upload("companies.csv", "ИНН;КПП;Название\n7707083893;770401001;ООО Ромашка\n".encode())
    assert len(rows) == 1
    assert "ИНН: 7707083893" in rows[0].value
    assert "КПП: 770401001" in rows[0].value


def test_unsupported_bulk_extension_is_rejected() -> None:
    try:
        parse_upload("companies.pdf", b"data")
    except ValueError as exc:
        assert "CSV" in str(exc)
    else:
        raise AssertionError("unsupported file was accepted")
