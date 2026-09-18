import io
import zipfile
from datetime import datetime
from xml.etree import ElementTree as ET

from legal_entity_agent.excel_export import (
    ExcelCheckRow,
    build_check_workbook,
    row_from_assessment,
    row_from_error,
)
from legal_entity_agent.models import Assessment, FnsEntityRecord, InaccuracyState, SearchQuery


def _texts(workbook: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    return [element.text or "" for element in root.iter() if element.tag.endswith("}t")]


def test_workbook_contains_requested_four_columns_and_result() -> None:
    assessment = Assessment(
        FnsEntityRecord(
            query=SearchQuery("ООО Ромашка Москва"),
            source_url="https://egrul.nalog.ru/",
            fetched_at=datetime.now().astimezone(),
            name="ООО Ромашка",
            inn="7707083893",
            ogrn="1027700132195",
            kpp="770401001",
            inaccuracy_state=InaccuracyState.ABSENT,
        )
    )

    workbook = build_check_workbook([row_from_assessment(assessment)])
    texts = _texts(workbook)

    assert texts[:4] == [
        "Номер",
        "Наименование юридического лица",
        "ИНН; ОГРН; КПП; иная информация для поиска",
        "Есть или нет недостоверности",
    ]
    assert "ООО Ромашка" in texts
    assert "ИНН: 7707083893; ОГРН: 1027700132195; КПП: 770401001; Запрос: ООО Ромашка Москва" in texts
    assert "Нет" in texts


def test_error_rows_are_exported() -> None:
    workbook = build_check_workbook([
        row_from_error("ИНН 7707083893", "тайм-аут ФНС"),
        ExcelCheckRow("—", "—", "Не распознано"),
    ])

    texts = _texts(workbook)

    assert "Проверка не выполнена: тайм-аут ФНС" in texts
    assert "Не распознано" in texts
