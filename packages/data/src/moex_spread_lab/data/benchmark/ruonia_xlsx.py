"""Excel (.xlsx) and HTML row parsers for Central Bank RUONIA dynamics."""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from typing import Any

RUONIA_HTML_PATTERN = re.compile(
    r"<td>\s*(\d{2}\.\d{2}\.\d{4})\s*</td>\s*<td[^>]*>\s*([\d,\.]+)\s*</td>",
    re.IGNORECASE,
)


def _extract_xlsx_pairs(rows_iter: Any, date_index: int, rate_index: int) -> list[tuple[Any, Any]]:
    result: list[tuple[Any, Any]] = []
    min_len = max(date_index, rate_index) + 1
    for row in rows_iter:
        if len(row) < min_len:
            continue
        row_date, rate = row[date_index], row[rate_index]
        if row_date is None or rate is None:
            continue
        parsed_date: Any = row_date if isinstance(row_date, (datetime, date)) else str(row_date)
        result.append((parsed_date, rate))
    return result


def read_ruonia_xlsx(raw: bytes) -> list[tuple[Any, Any]]:
    if not raw:
        return []
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except (ImportError, OSError, ValueError, KeyError):
        return []
    try:
        sheet = workbook.active
        if sheet is None:
            return []
        rows_iter = sheet.iter_rows(values_only=True)
        header = next(rows_iter, None)
        if not header:
            return []
        names = [str(value).strip() if value is not None else "" for value in header]
        if "DT" not in names or "ruo" not in names:
            return []
        return _extract_xlsx_pairs(rows_iter, names.index("DT"), names.index("ruo"))
    finally:
        workbook.close()


def parse_ruonia_pair(
    date_text: Any, rate_text: Any, start: date, finish: date
) -> tuple[str, dict[str, Any]] | None:
    try:
        if isinstance(date_text, datetime):
            row_date = date_text.date()
        elif isinstance(date_text, date):
            row_date = date_text
        else:
            row_date = date.fromisoformat("-".join(reversed(str(date_text).split("."))))
        rate = float(str(rate_text).replace(",", "."))
    except ValueError:
        return None
    if not (start <= row_date <= finish):
        return None
    iso = row_date.isoformat()
    return iso, {
        "date": iso,
        "rate": rate,
        "source_ticker": "CBR_RUONIA",
        "source_kind": "synthetic_proxy",
    }


# Preserve legacy private import names.
_read_ruonia_xlsx = read_ruonia_xlsx
_parse_ruonia_pair = parse_ruonia_pair
