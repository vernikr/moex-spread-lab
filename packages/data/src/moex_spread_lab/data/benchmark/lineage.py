"""Historical LQDT benchmark lineage with observed and proxy periods."""

from __future__ import annotations

import hashlib
import json
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

import certifi

from .lineage_math import (
    BENCHMARK_SCHEMA,
    CHAIN,
    HISTORY_PAGE_SIZE,
    MOEX_BASE,
    OBSERVED,
    RUSFAR_PAGE_SIZE,
    BenchmarkError,
    adjust_vtbm_split,
    build_lineage,
    rate_rows_to_price,
)
from .ruonia_xlsx import (
    RUONIA_HTML_PATTERN,
    _parse_ruonia_pair,
    _read_ruonia_xlsx,
)

TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())


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
    return value, hashlib.sha256(raw).hexdigest()


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
    return [
        dict(zip(columns, row, strict=False))
        for row in data
        if isinstance(row, list) and len(row) == len(columns)
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
