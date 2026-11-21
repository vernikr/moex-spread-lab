"""Historical LQDT benchmark lineage with observed and proxy periods."""

from __future__ import annotations

import hashlib
import io
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import certifi

from moex_spread_lab.core.w0 import W0Error


BENCHMARK_SCHEMA = "lqdt-lineage.v1"
MOEX_BASE = "https://iss.moex.com"
CHAIN = ("CBR_RUONIA", "FXMM", "RUSFAR", "VTBM", "LQDT")
OBSERVED = {"VTBM", "LQDT"}
HISTORY_PAGE_SIZE = 100
RUSFAR_PAGE_SIZE = 500
TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class BenchmarkError(W0Error):
    """Raised when a benchmark source or lineage cannot be normalized."""


def _request_json(url: str, timeout: float = 30.0) -> tuple[dict[str, Any], str]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "moex-spread-lab/0.1 benchmark-intake"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=TLS_CONTEXT) as response:
            raw = response.read()
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
            raw = response.read()
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
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        return b"", "", f"request_error:{type(exc).__name__}"
    return raw, hashlib.sha256(raw).hexdigest(), content_type


def _table_rows(block: dict[str, Any], name: str) -> list[dict[str, Any]]:
    columns = block.get("columns")
    data = block.get("data")
    if not isinstance(columns, list) or not isinstance(data, list):
        raise BenchmarkError(f"invalid benchmark table: {name}")
    return [dict(zip(columns, row)) for row in data if isinstance(row, list) and len(row) == len(columns)]


def fetch_moex_history(
    secid: str,
    from_date: str,
    till_date: str,
    *,
    delay_seconds: float = 0.1,
    board: str | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    page = 0
    hashes: list[str] = []
    while True:
        start = page * HISTORY_PAGE_SIZE
        query = urlencode({"iss.meta": "off", "from": from_date, "till": till_date, "start": start})
        board_path = f"/boards/{board}" if board else ""
        url = f"{MOEX_BASE}/iss/history/engines/stock/markets/shares{board_path}/securities/{secid}.json?{query}"
        payload, raw_hash = _request_json(url)
        hashes.append(raw_hash)
        batch = _table_rows(payload.get("history", {}), "history")
        rows.extend(batch)
        page += 1
        if len(batch) < HISTORY_PAGE_SIZE:
            break
        if delay_seconds:
            time.sleep(delay_seconds)
    normalized = []
    for row in rows:
        trade_date = row.get("TRADEDATE")
        close = next(
            (value for field in ("LEGALCLOSEPRICE", "CLOSE", "WAPRICE")
             if isinstance((value := row.get(field)), (int, float)) and value > 0),
            None,
        )
        if isinstance(trade_date, str) and isinstance(close, (int, float)) and close > 0:
            normalized.append({
                "date": trade_date,
                "price": float(close),
                "source_ticker": secid,
                "source_kind": "observed",
                "board": row.get("BOARDID"),
                "price_role": "legal_close" if row.get("LEGALCLOSEPRICE") and row["LEGALCLOSEPRICE"] > 0 else "last_trade_close",
            })
    deduplicated = {row["date"]: row for row in normalized}
    normalized = [deduplicated[key] for key in sorted(deduplicated)]
    return {
        "ticker": secid,
        "board": board,
        "from": from_date,
        "till": till_date,
        "source": "moex_iss_history",
        "page_count": page,
        "raw_response_hashes": hashes,
        "rows": normalized,
    }


def fetch_rusfar_rates(from_date: str, till_date: str, *, delay_seconds: float = 0.1) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    page = 0
    hashes: list[str] = []
    while True:
        start = page * RUSFAR_PAGE_SIZE
        query = urlencode({"iss.meta": "off", "interval": 24, "from": from_date, "till": till_date, "start": start})
        url = f"{MOEX_BASE}/iss/engines/stock/markets/index/boards/MMIX/securities/RUSFAR/candles.json?{query}"
        payload, raw_hash = _request_json(url)
        hashes.append(raw_hash)
        batch = _table_rows(payload.get("candles", {}), "RUSFAR candles")
        rows.extend(batch)
        page += 1
        if len(batch) < RUSFAR_PAGE_SIZE:
            break
        if delay_seconds:
            time.sleep(delay_seconds)
    normalized = []
    for row in rows:
        begin = row.get("begin")
        rate = row.get("close")
        if isinstance(begin, str) and isinstance(rate, (int, float)):
            normalized.append({"date": begin[:10], "rate": float(rate), "source_ticker": "RUSFAR", "source_kind": "synthetic_proxy"})
    normalized.sort(key=lambda row: row["date"])
    return {
        "ticker": "RUSFAR",
        "from": from_date,
        "till": till_date,
        "source": "moex_iss_candles_rate",
        "page_count": page,
        "raw_response_hashes": hashes,
        "rows": normalized,
    }


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
    pattern = re.compile(r"<td>\s*(\d{2}\.\d{2}\.\d{4})\s*</td>\s*<td[^>]*>\s*([\d,\.]+)\s*</td>", re.IGNORECASE)
    while cursor <= finish:
        chunk_end = min(cursor + timedelta(days=364), finish)
        from_dot = cursor.strftime("%d.%m.%Y")
        till_dot = chunk_end.strftime("%d.%m.%Y")
        query = urlencode({
            "UniDbQuery.Posted": "True",
            "UniDbQuery.From": from_dot,
            "UniDbQuery.To": till_dot,
        })
        html_url = f"https://www.cbr.ru/hd_base/ruonia/dynamics/?{query}"
        text, response_hash = _request_text(html_url, form={})
        request_count += 1
        response_hashes.append(response_hash)
        html_rows = pattern.findall(text)
        if html_rows:
            methods.append("html")
        else:
            excel_query = urlencode({
                "FromDate": cursor.strftime("%m/%d/%Y"),
                "ToDate": chunk_end.strftime("%m/%d/%Y"),
                "posted": "False",
                "backUrl": "/hd_base/ruonia/dynamics/",
            })
            excel_url = f"https://www.cbr.ru/Queries/UniDbQuery/DownloadExcel/14315?{excel_query}"
            raw, excel_hash, content_type = _request_bytes(excel_url)
            request_count += 1
            response_hashes.append(excel_hash)
            html_rows = _read_ruonia_xlsx(raw) if raw and "spreadsheet" in content_type.lower() else []
            methods.append("xlsx" if html_rows else "unavailable")
        for date_text, rate_text in html_rows:
            try:
                if isinstance(date_text, datetime):
                    row_date = date_text.date()
                else:
                    row_date = date.fromisoformat("-".join(reversed(str(date_text).split("."))))
                rate = float(str(rate_text).replace(",", "."))
            except ValueError:
                continue
            if start <= row_date <= finish:
                rows[row_date.isoformat()] = {
                    "date": row_date.isoformat(),
                    "rate": rate,
                    "source_ticker": "CBR_RUONIA",
                    "source_kind": "synthetic_proxy",
                }
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
        rows = sheet.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            return []
        names = [str(value).strip() if value is not None else "" for value in header]
        date_index = names.index("DT") if "DT" in names else -1
        rate_index = names.index("ruo") if "ruo" in names else -1
        if date_index < 0 or rate_index < 0:
            return []
        result = []
        for row in rows:
            if len(row) <= max(date_index, rate_index):
                continue
            row_date = row[date_index]
            rate = row[rate_index]
            if row_date is None or rate is None:
                continue
            if isinstance(row_date, datetime):
                parsed_date: Any = row_date
            elif isinstance(row_date, date):
                parsed_date = row_date
            else:
                parsed_date = str(row_date)
            result.append((parsed_date, rate))
        return result
    finally:
        workbook.close()


def rate_rows_to_price(rows: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    result = []
    price = 1000.0
    previous: date | None = None
    for row in sorted(rows, key=lambda item: item["date"]):
        current = date.fromisoformat(row["date"])
        days = (current - previous).days if previous else 1
        days = max(days, 1)
        price *= 1 + float(row["rate"]) / 100 * days / 365
        result.append({
            "date": row["date"],
            "price": price,
            "source_ticker": ticker,
            "source_kind": "synthetic_proxy",
            "price_role": "rate_compounded_proxy",
            "rate": row["rate"],
        })
        previous = current
    return result


def adjust_vtbm_split(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda item: item["date"])
    adjusted = [dict(row) for row in ordered]
    factor = 1.0
    for index in range(len(adjusted) - 1, -1, -1):
        adjusted[index]["price"] *= factor
        if index:
            previous = ordered[index - 1]["price"]
            current = ordered[index]["price"]
            if previous > 0 and current > 0:
                ratio = current / previous
                if ratio < 0.2 or ratio > 5:
                    factor *= ratio
        adjusted[index]["split_adjustment"] = factor
    return adjusted


def build_lineage(raw_sources: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    sources = {ticker: sorted(rows, key=lambda row: row["date"]) for ticker, rows in raw_sources.items() if rows}
    if "VTBM" in sources:
        sources["VTBM"] = adjust_vtbm_split(sources["VTBM"])
    returns_by_date: dict[str, dict[str, Any]] = {}
    for ticker in CHAIN:
        rows = sources.get(ticker, [])
        for previous, current in zip(rows, rows[1:]):
            if previous["price"] <= 0:
                continue
            ret = current["price"] / previous["price"] - 1
            if abs(ret) >= 0.2:
                continue
            existing = returns_by_date.get(current["date"])
            if existing is None or CHAIN.index(ticker) > CHAIN.index(existing["source_ticker"]):
                returns_by_date[current["date"]] = {"return": ret, "source_ticker": ticker}
    all_dates = sorted({row["date"] for rows in sources.values() for row in rows})
    if not all_dates:
        raise BenchmarkError("benchmark lineage has no rows")
    available_rows = [row for rows in sources.values() for row in rows]
    if not available_rows:
        raise BenchmarkError("benchmark lineage has no observed price source")
    latest_source_row = max(
        available_rows,
        key=lambda row: (row["date"], CHAIN.index(row["source_ticker"]) if row["source_ticker"] in CHAIN else -1),
    )
    current_price = latest_source_row["price"]
    prices: list[dict[str, Any]] = [{
        "date": latest_source_row["date"],
        "price": current_price,
        "source_ticker": latest_source_row["source_ticker"],
        "source_kind": "observed" if latest_source_row["source_ticker"] in OBSERVED else "synthetic_proxy",
    }]
    available_by_date: dict[str, str] = {}
    for ticker, rows in sources.items():
        for row in rows:
            previous = available_by_date.get(row["date"])
            if previous is None or CHAIN.index(ticker) > CHAIN.index(previous):
                available_by_date[row["date"]] = ticker
    for current_date in reversed(all_dates[:-1]):
        newer_date = prices[-1]["date"]
        ret_info = returns_by_date.get(newer_date)
        if ret_info:
            current_price /= 1 + ret_info["return"]
            source_ticker = ret_info["source_ticker"]
        else:
            source_ticker = available_by_date.get(current_date, "holiday_or_missing_return")
        source_kind = (
            "observed" if source_ticker in OBSERVED
            else "synthetic_proxy" if source_ticker in CHAIN
            else "unknown"
        )
        prices.append({"date": current_date, "price": current_price, "source_ticker": source_ticker, "source_kind": source_kind})
    prices.sort(key=lambda row: row["date"])
    observed_start = min((row["date"] for ticker in OBSERVED for row in sources.get(ticker, [])), default=None)
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
