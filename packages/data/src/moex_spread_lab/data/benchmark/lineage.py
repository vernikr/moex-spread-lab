"""Historical LQDT benchmark lineage: CBR RUONIA/MOEX intake, stitching and proxies."""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlencode

import certifi

from moex_spread_lab.core.w0 import W0Error

BENCHMARK_SCHEMA = "lqdt-lineage.v1"
MOEX_BASE = "https://iss.moex.com"
CHAIN = ("CBR_RUONIA", "FXMM", "RUSFAR", "VTBM", "LQDT")
OBSERVED = {"VTBM", "LQDT"}
HISTORY_PAGE_SIZE = 100
RUSFAR_PAGE_SIZE = 500
RUONIA_HTML_PATTERN = re.compile(
    r"<td>\s*(\d{2}\.\d{2}\.\d{4})\s*</td>\s*<td[^>]*>\s*([\d,\.]+)\s*</td>",
    re.IGNORECASE,
)
TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class BenchmarkError(W0Error):
    """Raised when a benchmark source or lineage cannot be normalized."""


def rate_rows_to_price(rows: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    price = 1000.0
    previous: date | None = None
    for row in sorted(rows, key=lambda item: item["date"]):
        current = date.fromisoformat(row["date"])
        days = max((current - previous).days if previous else 1, 1)
        price *= 1 + float(row["rate"]) / 100 * days / 365
        result.append(
            {
                "date": row["date"],
                "price": price,
                "source_ticker": ticker,
                "source_kind": "synthetic_proxy",
                "price_role": "rate_compounded_proxy",
                "rate": row["rate"],
            }
        )
        previous = current
    return result


def adjust_vtbm_split(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda item: item["date"])
    adjusted = [dict(row) for row in ordered]
    factor = 1.0
    for index in range(len(adjusted) - 1, -1, -1):
        adjusted[index]["price"] *= factor
        if index:
            previous = float(ordered[index - 1]["price"])
            current = float(ordered[index]["price"])
            if previous > 0 and current > 0:
                ratio = current / previous
                if ratio < 0.2 or ratio > 5:
                    factor *= ratio
        adjusted[index]["split_adjustment"] = factor
    return adjusted


def _collect_daily_returns(
    sources: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    returns_by_date: dict[str, dict[str, Any]] = {}
    for ticker in CHAIN:
        rows = sources.get(ticker, [])
        for previous, current in itertools.pairwise(rows):
            if float(previous["price"]) <= 0:
                continue
            ret = float(current["price"]) / float(previous["price"]) - 1
            if abs(ret) >= 0.2:
                continue
            existing = returns_by_date.get(current["date"])
            if existing is None or CHAIN.index(ticker) > CHAIN.index(existing["source_ticker"]):
                returns_by_date[current["date"]] = {"return": ret, "source_ticker": ticker}
    return returns_by_date


def _best_available_by_date(sources: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    available_by_date: dict[str, str] = {}
    for ticker, rows in sources.items():
        for row in rows:
            previous = available_by_date.get(row["date"])
            if previous is None or CHAIN.index(ticker) > CHAIN.index(previous):
                available_by_date[row["date"]] = ticker
    return available_by_date


def _source_kind_for_ticker(source_ticker: str) -> str:
    if source_ticker in OBSERVED:
        return "observed"
    if source_ticker in CHAIN:
        return "synthetic_proxy"
    return "unknown"


def _stitch_backward_prices(
    *,
    all_dates: list[str],
    latest_row: dict[str, Any],
    returns_by_date: dict[str, dict[str, Any]],
    available_by_date: dict[str, str],
) -> list[dict[str, Any]]:
    current_price = float(latest_row["price"])
    latest_ticker = str(latest_row["source_ticker"])
    prices: list[dict[str, Any]] = [
        {
            "date": latest_row["date"],
            "price": current_price,
            "source_ticker": latest_ticker,
            "source_kind": "observed" if latest_ticker in OBSERVED else "synthetic_proxy",
        }
    ]
    for current_date in reversed(all_dates[:-1]):
        newer_date = str(prices[-1]["date"])
        ret_info = returns_by_date.get(newer_date)
        if ret_info is not None:
            current_price /= 1 + float(ret_info["return"])
            source_ticker = str(ret_info["source_ticker"])
        else:
            source_ticker = available_by_date.get(current_date, "holiday_or_missing_return")
        prices.append(
            {
                "date": current_date,
                "price": current_price,
                "source_ticker": source_ticker,
                "source_kind": _source_kind_for_ticker(source_ticker),
            }
        )
    prices.sort(key=lambda row: str(row["date"]))
    return prices


def build_lineage(raw_sources: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    sources = {
        ticker: sorted(rows, key=lambda row: row["date"])
        for ticker, rows in raw_sources.items()
        if rows
    }
    if "VTBM" in sources:
        sources["VTBM"] = adjust_vtbm_split(sources["VTBM"])
    all_dates = sorted({str(row["date"]) for rows in sources.values() for row in rows})
    if not all_dates:
        raise BenchmarkError("benchmark lineage has no rows")
    available_rows = [row for rows in sources.values() for row in rows]
    latest_row = max(
        available_rows,
        key=lambda row: (
            row["date"],
            CHAIN.index(row["source_ticker"]) if row["source_ticker"] in CHAIN else -1,
        ),
    )
    prices = _stitch_backward_prices(
        all_dates=all_dates,
        latest_row=latest_row,
        returns_by_date=_collect_daily_returns(sources),
        available_by_date=_best_available_by_date(sources),
    )
    observed_start = min(
        (str(row["date"]) for ticker in OBSERVED for row in sources.get(ticker, [])),
        default=None,
    )
    return {
        "schema_version": BENCHMARK_SCHEMA,
        "chain": list(CHAIN),
        "observed_ticker_set": sorted(OBSERVED),
        "observed_start": observed_start,
        "extended_start": prices[0]["date"],
        "extended_end": prices[-1]["date"],
        "rows": prices,
        "source_rows": {ticker: len(rows) for ticker, rows in sources.items()},
        "interpretation": {
            "observed": "VTBM/LQDT exchange price observations",
            "synthetic_proxy": "rate-compounded or return-linked extension; not an observed LQDT price",
            "split_handling": "VTBM internal 1000:1 split adjusted before return stitching",
        },
    }


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


def _read_ruonia_xlsx(raw: bytes) -> list[tuple[Any, Any]]:
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


def _parse_ruonia_pair(
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


def _request_json(url: str, timeout: float = 30.0) -> tuple[dict[str, Any], str]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "moex-spread-lab/0.1 benchmark-intake",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=TLS_CONTEXT) as response:
            raw: bytes = response.read()
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise BenchmarkError(f"benchmark request failed: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"benchmark response is not JSON: {url}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"benchmark JSON root is not an object: {url}")
    return cast(dict[str, Any], value), hashlib.sha256(raw).hexdigest()


def _request_text(
    url: str,
    *,
    form: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[str, str]:
    body = urlencode(form).encode("utf-8") if form else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "moex-spread-lab/0.1 benchmark-intake",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=TLS_CONTEXT) as response:
            raw: bytes = response.read()
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        return "", f"request_error:{type(exc).__name__}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    return text, hashlib.sha256(raw).hexdigest()


def _request_bytes(url: str, *, timeout: float = 30.0) -> tuple[bytes, str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
            "User-Agent": "moex-spread-lab/0.1 benchmark-intake",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=TLS_CONTEXT) as response:
            raw: bytes = response.read()
            content_type = str(response.headers.get("Content-Type", ""))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        return b"", "", f"request_error:{type(exc).__name__}"
    return raw, hashlib.sha256(raw).hexdigest(), content_type


def _table_rows(block: dict[str, Any], name: str) -> list[dict[str, Any]]:
    columns = block.get("columns")
    data = block.get("data")
    if not isinstance(columns, list) or not isinstance(data, list):
        raise BenchmarkError(f"invalid benchmark table: {name}")
    typed_columns = cast(list[str], columns)
    typed_data = cast(list[Any], data)
    return [
        dict(zip(typed_columns, cast(list[Any], row), strict=False))
        for row in typed_data
        if isinstance(row, list) and len(cast(list[Any], row)) == len(typed_columns)
    ]


def _normalize_moex_history_row(row: dict[str, Any], secid: str) -> dict[str, Any] | None:
    trade_date = row.get("TRADEDATE")
    close = next(
        (
            value
            for field in ("LEGALCLOSEPRICE", "CLOSE", "WAPRICE")
            if isinstance((value := row.get(field)), (int, float)) and value > 0
        ),
        None,
    )
    if not isinstance(trade_date, str) or not isinstance(close, (int, float)) or close <= 0:
        return None
    legal_close = row.get("LEGALCLOSEPRICE")
    is_legal = isinstance(legal_close, (int, float)) and legal_close > 0
    return {
        "date": trade_date,
        "price": float(close),
        "source_ticker": secid,
        "source_kind": "observed",
        "board": row.get("BOARDID"),
        "price_role": "legal_close" if is_legal else "last_trade_close",
    }


def fetch_moex_history(
    secid: str,
    from_date: str,
    till_date: str,
    *,
    delay_seconds: float = 0.1,
    board: str | None = None,
) -> dict[str, Any]:
    raw_rows: list[dict[str, Any]] = []
    page = 0
    hashes: list[str] = []
    board_path = f"/boards/{board}" if board else ""
    while True:
        query = urlencode(
            {
                "iss.meta": "off",
                "from": from_date,
                "till": till_date,
                "start": page * HISTORY_PAGE_SIZE,
            }
        )
        url = (
            f"{MOEX_BASE}/iss/history/engines/stock/markets/shares{board_path}"
            f"/securities/{secid}.json?{query}"
        )
        payload, raw_hash = _request_json(url)
        hashes.append(raw_hash)
        batch = _table_rows(payload.get("history", {}), "history")
        raw_rows.extend(batch)
        page += 1
        if len(batch) < HISTORY_PAGE_SIZE:
            break
        if delay_seconds:
            time.sleep(delay_seconds)
    deduplicated: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        item = _normalize_moex_history_row(row, secid)
        if item is not None:
            deduplicated[str(item["date"])] = item
    return {
        "ticker": secid,
        "board": board,
        "from": from_date,
        "till": till_date,
        "source": "moex_iss_history",
        "page_count": page,
        "raw_response_hashes": hashes,
        "rows": [deduplicated[key] for key in sorted(deduplicated)],
    }


def fetch_rusfar_rates(
    from_date: str, till_date: str, *, delay_seconds: float = 0.1
) -> dict[str, Any]:
    raw_rows: list[dict[str, Any]] = []
    page = 0
    hashes: list[str] = []
    while True:
        query = urlencode(
            {
                "iss.meta": "off",
                "interval": 24,
                "from": from_date,
                "till": till_date,
                "start": page * RUSFAR_PAGE_SIZE,
            }
        )
        url = f"{MOEX_BASE}/iss/engines/stock/markets/index/boards/MMIX/securities/RUSFAR/candles.json?{query}"
        payload, raw_hash = _request_json(url)
        hashes.append(raw_hash)
        batch = _table_rows(payload.get("candles", {}), "RUSFAR candles")
        raw_rows.extend(batch)
        page += 1
        if len(batch) < RUSFAR_PAGE_SIZE:
            break
        if delay_seconds:
            time.sleep(delay_seconds)
    normalized = [
        {
            "date": begin[:10],
            "rate": float(rate),
            "source_ticker": "RUSFAR",
            "source_kind": "synthetic_proxy",
        }
        for row in raw_rows
        if isinstance((begin := row.get("begin")), str)
        and isinstance((rate := row.get("close")), (int, float))
    ]
    normalized.sort(key=lambda row: str(row["date"]))
    return {
        "ticker": "RUSFAR",
        "from": from_date,
        "till": till_date,
        "source": "moex_iss_candles_rate",
        "page_count": page,
        "raw_response_hashes": hashes,
        "rows": normalized,
    }


def _fetch_ruonia_chunk(
    cursor: date, chunk_end: date
) -> tuple[list[tuple[Any, Any]], list[str], str, int]:
    query = urlencode(
        {
            "UniDbQuery.Posted": "True",
            "UniDbQuery.From": cursor.strftime("%d.%m.%Y"),
            "UniDbQuery.To": chunk_end.strftime("%d.%m.%Y"),
        }
    )
    text, response_hash = _request_text(
        f"https://www.cbr.ru/hd_base/ruonia/dynamics/?{query}", form={}
    )
    pairs: list[tuple[Any, Any]] = list(RUONIA_HTML_PATTERN.findall(text))
    if pairs:
        return pairs, [response_hash], "html", 1
    excel_query = urlencode(
        {
            "FromDate": cursor.strftime("%m/%d/%Y"),
            "ToDate": chunk_end.strftime("%m/%d/%Y"),
            "posted": "False",
            "backUrl": "/hd_base/ruonia/dynamics/",
        }
    )
    raw, excel_hash, content_type = _request_bytes(
        f"https://www.cbr.ru/Queries/UniDbQuery/DownloadExcel/14315?{excel_query}"
    )
    xlsx_pairs = _read_ruonia_xlsx(raw) if raw and "spreadsheet" in content_type.lower() else []
    return xlsx_pairs, [response_hash, excel_hash], ("xlsx" if xlsx_pairs else "unavailable"), 2


def fetch_cbr_ruonia_rates(
    from_date: str,
    till_date: str,
    *,
    delay_seconds: float = 0.2,
) -> dict[str, Any]:
    """Fetch RUONIA in bounded yearly HTML requests with a truthful empty result."""
    start = date.fromisoformat(from_date)
    finish = date.fromisoformat(till_date)
    rows: dict[str, dict[str, Any]] = {}
    response_hashes: list[str] = []
    methods: list[str] = []
    request_count = 0
    cursor = start
    while cursor <= finish:
        chunk_end = min(cursor + timedelta(days=364), finish)
        pairs, chunk_hashes, method, reqs = _fetch_ruonia_chunk(cursor, chunk_end)
        request_count += reqs
        response_hashes.extend(chunk_hashes)
        methods.append(method)
        for date_text, rate_text in pairs:
            parsed = _parse_ruonia_pair(date_text, rate_text, start, finish)
            if parsed is not None:
                rows[parsed[0]] = parsed[1]
        cursor = chunk_end + timedelta(days=1)
        if cursor <= finish and delay_seconds:
            time.sleep(delay_seconds)
    normalized = [rows[key] for key in sorted(rows)]
    return {
        "ticker": "CBR_RUONIA",
        "from": from_date,
        "till": till_date,
        "source": "cbr_ruonia_html_or_xlsx",
        "request_count": request_count,
        "methods": methods,
        "raw_response_hashes": response_hashes,
        "status": "available" if normalized else "unavailable_or_unparsed",
        "rows": normalized,
    }


__all__ = [
    "BENCHMARK_SCHEMA",
    "CHAIN",
    "HISTORY_PAGE_SIZE",
    "MOEX_BASE",
    "OBSERVED",
    "RUSFAR_PAGE_SIZE",
    "TLS_CONTEXT",
    "BenchmarkError",
    "_read_ruonia_xlsx",
    "_request_bytes",
    "_request_json",
    "_request_text",
    "adjust_vtbm_split",
    "build_lineage",
    "fetch_cbr_ruonia_rates",
    "fetch_moex_history",
    "fetch_rusfar_rates",
    "rate_rows_to_price",
]
