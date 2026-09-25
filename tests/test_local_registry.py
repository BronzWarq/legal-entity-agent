from datetime import UTC, datetime

import pytest

from legal_entity_agent.local_registry import LocalEgrulClient


@pytest.mark.asyncio
async def test_local_registry_reads_company_by_inn(tmp_path) -> None:
    database = tmp_path / "mcp_egrul_data.sqlite"
    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE companies (
                inn TEXT PRIMARY KEY, ogrn TEXT NOT NULL, kpp TEXT,
                name_short TEXT, name_full TEXT, status TEXT,
                registered_at TEXT, address_legal TEXT,
                source_date TEXT, data_json TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO companies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "7707083893",
                "1027700132195",
                "770401001",
                "ООО Ромашка",
                "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ РОМАШКА",
                "active",
                "2002-01-01",
                "г. Москва",
                "2026-09-24",
                '{"okved_main_code":"62.01"}',
            ),
        )

    record = await LocalEgrulClient(database).lookup("7707083893")

    assert record is not None
    assert record.inn == "7707083893"
    assert record.ogrn == "1027700132195"
    assert record.name == "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ РОМАШКА"
    assert record.registration_date == datetime(2002, 1, 1, tzinfo=UTC).date()
    assert record.raw["source_date"] == "2026-09-24"


@pytest.mark.asyncio
async def test_local_registry_does_not_turn_missing_record_into_success(tmp_path) -> None:
    database = tmp_path / "empty.sqlite"
    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE companies (inn TEXT PRIMARY KEY, ogrn TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE individual_entrepreneurs (inn TEXT PRIMARY KEY, ogrnip TEXT NOT NULL)"
        )

    assert await LocalEgrulClient(database).lookup("7707083893") is None
