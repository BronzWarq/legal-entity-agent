"""Разбор CSV/XLSX-файлов со списком юридических лиц без сторонних библиотек."""
from __future__ import annotations

import csv
import io
import zipfile
from xml.etree import ElementTree as ET

from .models import SearchQuery


def _query_from_row(row: list[str], headers: list[str]) -> SearchQuery | None:
    if not any(value.strip() for value in row):
        return None
    pairs = []
    for header, value in zip(headers, row):
        if not value.strip():
            continue
        h = header.casefold()
        if any(token in h for token in ("инн", "огрн", "кпп", "назван", "адрес")):
            pairs.append(f"{header}: {value.strip()}")
    return SearchQuery("; ".join(pairs) if pairs else "; ".join(v.strip() for v in row if v.strip()))


def parse_csv(data: bytes) -> list[SearchQuery]:
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return []
    headers = [item.strip() or f"поле {i}" for i, item in enumerate(rows[0], 1)]
    return [query for row in rows[1:] if (query := _query_from_row(row, headers))]


def parse_xlsx(data: bytes) -> list[SearchQuery]:
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in root.findall(f"{{{ns}}}si")]
        sheet_name = next(name for name in archive.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
        root = ET.fromstring(archive.read(sheet_name))
        rows: list[list[str]] = []
        for row in root.findall(f".//{{{ns}}}row"):
            values: list[str] = []
            for cell in row.findall(f"{{{ns}}}c"):
                value = cell.find(f"{{{ns}}}v")
                inline = cell.find(f"{{{ns}}}is")
                text = "".join(inline.itertext()) if inline is not None else (value.text if value is not None else "")
                if cell.get("t") == "s" and text.isdigit() and int(text) < len(shared):
                    text = shared[int(text)]
                values.append(text or "")
            rows.append(values)
    if not rows:
        return []
    headers = [item.strip() or f"поле {i}" for i, item in enumerate(rows[0], 1)]
    return [query for row in rows[1:] if (query := _query_from_row(row, headers))]


def parse_upload(filename: str, data: bytes) -> list[SearchQuery]:
    lower = filename.casefold()
    if lower.endswith((".csv", ".tsv")):
        return parse_csv(data)
    if lower.endswith(".xlsx"):
        return parse_xlsx(data)
    raise ValueError("поддерживаются только CSV и XLSX")
