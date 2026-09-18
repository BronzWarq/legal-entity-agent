"""Формирование компактного Excel-отчёта по результатам проверки."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from xml.etree import ElementTree as ET

from .models import Assessment, InaccuracyState

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_PROPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
_CORE_PROPS_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_DCTERMS_NS = "http://purl.org/dc/terms/"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

HEADERS = (
    "Номер",
    "Наименование юридического лица",
    "ИНН; ОГРН; КПП; иная информация для поиска",
    "Есть или нет недостоверности",
)


@dataclass(frozen=True, slots=True)
class ExcelCheckRow:
    """Одна строка пользовательского Excel-отчёта."""

    name: str
    lookup: str
    inaccuracy: str


def _inaccuracy_label(state: InaccuracyState) -> str:
    return {
        InaccuracyState.PRESENT: "Да",
        InaccuracyState.ABSENT: "Нет",
        InaccuracyState.NOT_REPORTED: "Не распознано",
    }[state]


def _lookup_text(assessment: Assessment) -> str:
    record = assessment.record
    parts = [
        f"ИНН: {record.inn}" if record.inn else None,
        f"ОГРН: {record.ogrn}" if record.ogrn else None,
        f"КПП: {record.kpp}" if record.kpp else None,
        f"Запрос: {record.query.value}" if record.query.value else None,
    ]
    return "; ".join(part for part in parts if part) or "—"


def row_from_assessment(assessment: Assessment) -> ExcelCheckRow:
    record = assessment.record
    return ExcelCheckRow(
        name=record.name or "—",
        lookup=_lookup_text(assessment),
        inaccuracy=_inaccuracy_label(record.inaccuracy_state),
    )


def row_from_error(query: str, error: str) -> ExcelCheckRow:
    return ExcelCheckRow(
        name="—",
        lookup=f"Запрос: {query}" if query else "—",
        inaccuracy=f"Проверка не выполнена: {error}",
    )


def build_check_workbook(rows: Iterable[ExcelCheckRow]) -> bytes:
    """Возвращает XLSX-файл с четырьмя колонками отчёта."""

    data = [HEADERS]
    data.extend(
        (str(index), row.name, row.lookup, row.inaccuracy)
        for index, row in enumerate(rows, start=1)
    )
    return _build_xlsx(data)


def _column_letter(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell_ref(row: int, column: int) -> str:
    return f"{_column_letter(column)}{row}"


def _text_cell(parent: ET.Element, ref: str, value: str, style: int) -> None:
    cell = ET.SubElement(parent, f"{{{_MAIN_NS}}}c", {"r": ref, "t": "inlineStr", "s": str(style)})
    inline = ET.SubElement(cell, f"{{{_MAIN_NS}}}is")
    text = ET.SubElement(inline, f"{{{_MAIN_NS}}}t")
    if value[:1].isspace() or value[-1:].isspace():
        text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    text.text = value


def _build_xlsx(data: list[tuple[str, ...]]) -> bytes:
    ET.register_namespace("", _MAIN_NS)
    ET.register_namespace("r", _REL_NS)
    ET.register_namespace("cp", _CORE_PROPS_NS)
    ET.register_namespace("dc", _DC_NS)
    ET.register_namespace("dcterms", _DCTERMS_NS)
    ET.register_namespace("xsi", _XSI_NS)

    sheet_data = ET.Element(f"{{{_MAIN_NS}}}worksheet")
    views = ET.SubElement(sheet_data, f"{{{_MAIN_NS}}}sheetViews")
    sheet_view = ET.SubElement(
        views,
        f"{{{_MAIN_NS}}}sheetView",
        {"workbookViewId": "0"},
    )
    ET.SubElement(
        sheet_view,
        f"{{{_MAIN_NS}}}pane",
        {"ySplit": "1", "topLeftCell": "A2", "activePane": "bottomLeft", "state": "frozen"},
    )
    sheet_format = ET.SubElement(sheet_data, f"{{{_MAIN_NS}}}sheetFormatPr", {"defaultRowHeight": "18"})
    sheet_format.set("defaultColWidth", "12")
    cols = ET.SubElement(sheet_data, f"{{{_MAIN_NS}}}cols")
    for index, width in enumerate((9, 34, 70, 30), start=1):
        ET.SubElement(cols, f"{{{_MAIN_NS}}}col", {"min": str(index), "max": str(index), "width": str(width), "customWidth": "1"})
    sheet_rows = ET.SubElement(sheet_data, f"{{{_MAIN_NS}}}sheetData")
    for row_number, values in enumerate(data, start=1):
        row = ET.SubElement(sheet_rows, f"{{{_MAIN_NS}}}row", {"r": str(row_number)})
        for column_number, value in enumerate(values, start=1):
            _text_cell(row, _cell_ref(row_number, column_number), value, 1 if row_number == 1 else 2)
    if len(data) > 1:
        ET.SubElement(sheet_data, f"{{{_MAIN_NS}}}autoFilter", {"ref": f"A1:D{len(data)}"})

    styles = ET.Element(f"{{{_MAIN_NS}}}styleSheet")
    fonts = ET.SubElement(styles, f"{{{_MAIN_NS}}}fonts", {"count": "2"})
    default_font = ET.SubElement(fonts, f"{{{_MAIN_NS}}}font")
    ET.SubElement(default_font, f"{{{_MAIN_NS}}}sz", {"val": "11"})
    ET.SubElement(default_font, f"{{{_MAIN_NS}}}name", {"val": "Arial"})
    header_font = ET.SubElement(fonts, f"{{{_MAIN_NS}}}font")
    ET.SubElement(header_font, f"{{{_MAIN_NS}}}sz", {"val": "11"})
    ET.SubElement(header_font, f"{{{_MAIN_NS}}}name", {"val": "Arial"})
    ET.SubElement(header_font, f"{{{_MAIN_NS}}}b")
    fills = ET.SubElement(styles, f"{{{_MAIN_NS}}}fills", {"count": "3"})
    default_fill = ET.SubElement(fills, f"{{{_MAIN_NS}}}fill")
    ET.SubElement(default_fill, f"{{{_MAIN_NS}}}patternFill", {"patternType": "none"})
    gray_fill = ET.SubElement(fills, f"{{{_MAIN_NS}}}fill")
    ET.SubElement(gray_fill, f"{{{_MAIN_NS}}}patternFill", {"patternType": "gray125"})
    header_fill = ET.SubElement(fills, f"{{{_MAIN_NS}}}fill")
    pattern = ET.SubElement(header_fill, f"{{{_MAIN_NS}}}patternFill", {"patternType": "solid"})
    ET.SubElement(pattern, f"{{{_MAIN_NS}}}fgColor", {"rgb": "D9EAF7"})
    ET.SubElement(pattern, f"{{{_MAIN_NS}}}bgColor", {"indexed": "64"})
    borders = ET.SubElement(styles, f"{{{_MAIN_NS}}}borders", {"count": "1"})
    border = ET.SubElement(borders, f"{{{_MAIN_NS}}}border")
    for side in ("left", "right", "top", "bottom", "diagonal"):
        ET.SubElement(border, f"{{{_MAIN_NS}}}{side}")
    cell_style_xfs = ET.SubElement(styles, f"{{{_MAIN_NS}}}cellStyleXfs", {"count": "1"})
    ET.SubElement(cell_style_xfs, f"{{{_MAIN_NS}}}xf", {"numFmtId": "0", "fontId": "0", "fillId": "0", "borderId": "0"})
    cell_xfs = ET.SubElement(styles, f"{{{_MAIN_NS}}}cellXfs", {"count": "3"})
    ET.SubElement(cell_xfs, f"{{{_MAIN_NS}}}xf", {"numFmtId": "0", "fontId": "0", "fillId": "0", "borderId": "0", "xfId": "0"})
    header_xf = ET.SubElement(cell_xfs, f"{{{_MAIN_NS}}}xf", {"numFmtId": "0", "fontId": "1", "fillId": "2", "borderId": "0", "xfId": "0", "applyFont": "1", "applyFill": "1", "applyAlignment": "1"})
    ET.SubElement(header_xf, f"{{{_MAIN_NS}}}alignment", {"horizontal": "center", "vertical": "center", "wrapText": "1"})
    body_xf = ET.SubElement(cell_xfs, f"{{{_MAIN_NS}}}xf", {"numFmtId": "0", "fontId": "0", "fillId": "0", "borderId": "0", "xfId": "0", "applyAlignment": "1"})
    ET.SubElement(body_xf, f"{{{_MAIN_NS}}}alignment", {"vertical": "top", "wrapText": "1"})
    cell_styles = ET.SubElement(styles, f"{{{_MAIN_NS}}}cellStyles", {"count": "1"})
    ET.SubElement(cell_styles, f"{{{_MAIN_NS}}}cellStyle", {"name": "Normal", "xfId": "0", "builtinId": "0"})

    workbook = ET.Element(f"{{{_MAIN_NS}}}workbook")
    ET.SubElement(workbook, f"{{{_MAIN_NS}}}sheets")
    sheets = workbook.find(f"{{{_MAIN_NS}}}sheets")
    ET.SubElement(sheets, f"{{{_MAIN_NS}}}sheet", {"name": "Проверка", "sheetId": "1", f"{{{_REL_NS}}}id": "rId1"})
    ET.SubElement(workbook, f"{{{_MAIN_NS}}}calcPr", {"calcId": "191029", "fullCalcOnLoad": "1"})

    workbook_rels = ET.Element(f"{{{_PKG_REL_NS}}}Relationships")
    ET.SubElement(workbook_rels, f"{{{_PKG_REL_NS}}}Relationship", {"Id": "rId1", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet", "Target": "worksheets/sheet1.xml"})
    ET.SubElement(workbook_rels, f"{{{_PKG_REL_NS}}}Relationship", {"Id": "rId2", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles", "Target": "styles.xml"})

    package_rels = ET.Element(f"{{{_PKG_REL_NS}}}Relationships")
    ET.SubElement(package_rels, f"{{{_PKG_REL_NS}}}Relationship", {"Id": "rId1", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument", "Target": "xl/workbook.xml"})
    ET.SubElement(package_rels, f"{{{_PKG_REL_NS}}}Relationship", {"Id": "rId2", "Type": "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", "Target": "docProps/core.xml"})
    ET.SubElement(package_rels, f"{{{_PKG_REL_NS}}}Relationship", {"Id": "rId3", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties", "Target": "docProps/app.xml"})

    content_types = ET.Element("{http://schemas.openxmlformats.org/package/2006/content-types}Types")
    ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    ET.register_namespace("", ct_ns)
    ET.SubElement(content_types, f"{{{ct_ns}}}Default", {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"})
    ET.SubElement(content_types, f"{{{ct_ns}}}Default", {"Extension": "xml", "ContentType": "application/xml"})
    ET.SubElement(content_types, f"{{{ct_ns}}}Override", {"PartName": "/xl/workbook.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"})
    ET.SubElement(content_types, f"{{{ct_ns}}}Override", {"PartName": "/xl/worksheets/sheet1.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"})
    ET.SubElement(content_types, f"{{{ct_ns}}}Override", {"PartName": "/xl/styles.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"})
    ET.SubElement(content_types, f"{{{ct_ns}}}Override", {"PartName": "/docProps/core.xml", "ContentType": "application/vnd.openxmlformats-package.core-properties+xml"})
    ET.SubElement(content_types, f"{{{ct_ns}}}Override", {"PartName": "/docProps/app.xml", "ContentType": "application/vnd.openxmlformats-officedocument.extended-properties+xml"})

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    core = ET.Element(f"{{{_CORE_PROPS_NS}}}coreProperties")
    ET.SubElement(core, f"{{{_DC_NS}}}creator").text = "legal-entity-agent"
    ET.SubElement(core, f"{{{_DCTERMS_NS}}}created", {f"{{{_XSI_NS}}}type": "dcterms:W3CDTF"}).text = now
    app = ET.Element(f"{{{_DOC_PROPS_NS}}}Properties")
    ET.SubElement(app, f"{{{_DOC_PROPS_NS}}}Application").text = "legal-entity-agent"

    def xml_bytes(element: ET.Element) -> bytes:
        return ET.tostring(element, encoding="utf-8", xml_declaration=True)

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", xml_bytes(content_types))
        archive.writestr("_rels/.rels", xml_bytes(package_rels))
        archive.writestr("xl/workbook.xml", xml_bytes(workbook))
        archive.writestr("xl/_rels/workbook.xml.rels", xml_bytes(workbook_rels))
        archive.writestr("xl/worksheets/sheet1.xml", xml_bytes(sheet_data))
        archive.writestr("xl/styles.xml", xml_bytes(styles))
        archive.writestr("docProps/core.xml", xml_bytes(core))
        archive.writestr("docProps/app.xml", xml_bytes(app))
    return output.getvalue()
