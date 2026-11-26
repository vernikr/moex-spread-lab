"""Historical LQDT benchmark lineage: CBR RUONIA/MOEX intake, stitching and proxies."""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import re
import ssl
import time
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlencode

import certifi

from moex_spread_lab.core.w0 import W0Error

BENCHMARK_SCHEMA = "lqdt-lineage.v1"
MOEX_BASE = "https://iss.moex.com"
CBR_BASE = "https://www.cbr.ru"
CHAIN = ("CBR_RUONIA", "FXMM", "RUSFAR", "VTBM", "LQDT")
OBSERVED = {"VTBM", "LQDT"}
RANK = {ticker: index for index, ticker in enumerate(CHAIN)}
HISTORY_PAGE_SIZE = 100
RUSFAR_PAGE_SIZE = 500
RUONIA_HTML_PATTERN = re.compile(
    r"<td>\s*(\d{2}\.\d{2}\.\d{4})\s*</td>\s*<td[^>]*>\s*([\d,\.]+)\s*</td>", re.IGNORECASE
)
TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())
USER_AGENT = "moex-spread-lab/0.1 benchmark-intake"


class BenchmarkError(W0Error):
    """Raised when a benchmark source or lineage cannot be normalized."""


def _price_row(day: Any, price: float, ticker: str) -> dict[str, Any]:
    kind = "observed" if ticker in OBSERVED else "synthetic_proxy" if ticker in CHAIN else "unknown"
    return {"date": day, "price": price, "source_ticker": ticker, "source_kind": kind}


def rate_rows_to_price(rows: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    """Compound percent rates over the calendar days elapsed since the previous row (ACT/365)."""
    result: list[dict[str, Any]] = []
    price = 1000.0
    previous: date | None = None
    for row in sorted(rows, key=lambda item: item["date"]):
        current = date.fromisoformat(row["date"])
        days = max((current - previous).days, 1) if previous else 1
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
    """Rescale every price before a >5x or <0.2x break (the 1000:1 split) onto the latest scale."""
    ordered = sorted(rows, key=lambda item: item["date"])
    adjusted = [dict(row) for row in ordered]
    factor = 1.0
    for index in range(len(adjusted) - 1, -1, -1):
        adjusted[index]["price"] *= factor
        if index:
            previous = float(ordered[index - 1]["price"])
            current = float(ordered[index]["price"])
            if previous > 0 and current > 0 and not 0.2 <= current / previous <= 5:
                factor *= current / previous
        adjusted[index]["split_adjustment"] = factor
    return adjusted


def _daily_returns(sources: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Daily return per date from the highest-ranked source; |r| >= 20% breaks are skipped."""
    returns: dict[str, dict[str, Any]] = {}
    for ticker in CHAIN:
        for previous, current in itertools.pairwise(sources.get(ticker, [])):
            if float(previous["price"]) <= 0:
                continue
            ret = float(current["price"]) / float(previous["price"]) - 1
            if abs(ret) < 0.2:
                returns[current["date"]] = {"return": ret, "source_ticker": ticker}
    return returns


def build_lineage(raw_sources: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Stitch the chain backwards from the latest price using the best daily return per date."""
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
    available = {row["date"]: ticker for ticker in CHAIN for row in sources.get(ticker, [])}
    returns = _daily_returns(sources)
    latest = max(
        (row for rows in sources.values() for row in rows),
        key=lambda row: (row["date"], RANK.get(row["source_ticker"], -1)),
    )
    price, ticker = float(latest["price"]), str(latest["source_ticker"])
    prices = [_price_row(latest["date"], price, ticker)]
    for current_date in reversed(all_dates[:-1]):
        ret_info = returns.get(str(prices[-1]["date"]))
        if ret_info is None:
            ticker = available.get(current_date, "holiday_or_missing_return")
        else:
            price /= 1 + float(ret_info["return"])
            ticker = str(ret_info["source_ticker"])
        prices.append(_price_row(current_date, price, ticker))
    prices.reverse()
    observed_start = min(
        (str(row["date"]) for ticker in OBSERVED for row in sources.get(ticker, [])), default=None
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


def _fetch(url: str, accept: str) -> tuple[bytes, str, str]:
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30.0, context=TLS_CONTEXT) as response:
            raw: bytes = response.read()
            content_type = str(response.headers.get("Content-Type", ""))
    except OSError as exc:
        raise BenchmarkError(f"benchmark request failed: {exc}") from exc
    return raw, hashlib.sha256(raw).hexdigest(), content_type


def _fetch_or_empty(url: str, accept: str) -> tuple[bytes, str, str]:
    """CBR intake stays truthful: a failed request is recorded, never retried or invented."""
    try:
        return _fetch(url, accept)
    except BenchmarkError as exc:
        return b"", f"request_error:{type(exc.__cause__).__name__}", ""


def _request_json(url: str) -> tuple[dict[str, Any], str]:
    raw, digest, _ = _fetch(url, "application/json")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"benchmark response is not JSON: {url}") from exc
    if not isinstance(payload, dict):
        raise BenchmarkError(f"benchmark JSON root is not an object: {url}")
    return cast(dict[str, Any], payload), digest


def _table_rows(block: dict[str, Any], name: str) -> list[dict[str, Any]]:
    columns = block.get("columns")
    data = block.get("data")
    if not isinstance(columns, list) or not isinstance(data, list):
        raise BenchmarkError(f"invalid benchmark table: {name}")
    typed_columns = cast(list[str], columns)
    return [
        dict(zip(typed_columns, cast(list[Any], row), strict=False))
        for row in cast(list[Any], data)
        if isinstance(row, list) and len(cast(list[Any], row)) == len(typed_columns)
    ]


def _fetch_iss_table(
    path: str, params: dict[str, Any], table: str, page_size: int, delay_seconds: float
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    hashes: list[str] = []
    page = 0
    while True:
        query = urlencode({"iss.meta": "off", **params, "start": page * page_size})
        payload, digest = _request_json(f"{MOEX_BASE}{path}.json?{query}")
        hashes.append(digest)
        batch = _table_rows(payload.get(table, {}), table)
        rows.extend(batch)
        page += 1
        if len(batch) < page_size:
            return rows, {"page_count": page, "raw_response_hashes": hashes}
        if delay_seconds:
            time.sleep(delay_seconds)


def _normalize_moex_history_row(row: dict[str, Any], secid: str) -> dict[str, Any] | None:
    trade_date = row.get("TRADEDATE")
    for field in ("LEGALCLOSEPRICE", "CLOSE", "WAPRICE"):
        value = row.get(field)
        if isinstance(trade_date, str) and isinstance(value, (int, float)) and value > 0:
            return {
                "date": trade_date,
                "price": float(value),
                "source_ticker": secid,
                "source_kind": "observed",
                "board": row.get("BOARDID"),
                "price_role": "legal_close" if field == "LEGALCLOSEPRICE" else "last_trade_close",
            }
    return None


def fetch_moex_history(
    secid: str,
    from_date: str,
    till_date: str,
    *,
    delay_seconds: float = 0.1,
    board: str | None = None,
) -> dict[str, Any]:
    board_path = f"/boards/{board}" if board else ""
    rows, meta = _fetch_iss_table(
        f"/iss/history/engines/stock/markets/shares{board_path}/securities/{secid}",
        {"from": from_date, "till": till_date},
        "history",
        HISTORY_PAGE_SIZE,
        delay_seconds,
    )
    by_date = {
        str(item["date"]): item
        for row in rows
        if (item := _normalize_moex_history_row(row, secid)) is not None
    }
    return {
        "ticker": secid,
        "board": board,
        "from": from_date,
        "till": till_date,
        "source": "moex_iss_history",
        **meta,
        "rows": [by_date[key] for key in sorted(by_date)],
    }


def fetch_rusfar_rates(
    from_date: str, till_date: str, *, delay_seconds: float = 0.1
) -> dict[str, Any]:
    rows, meta = _fetch_iss_table(
        "/iss/engines/stock/markets/index/boards/MMIX/securities/RUSFAR/candles",
        {"interval": 24, "from": from_date, "till": till_date},
        "candles",
        RUSFAR_PAGE_SIZE,
        delay_seconds,
    )
    normalized = [
        {
            "date": begin[:10],
            "rate": float(rate),
            "source_ticker": "RUSFAR",
            "source_kind": "synthetic_proxy",
        }
        for row in rows
        if isinstance((begin := row.get("begin")), str)
        and isinstance((rate := row.get("close")), (int, float))
    ]
    normalized.sort(key=lambda row: str(row["date"]))
    return {
        "ticker": "RUSFAR",
        "from": from_date,
        "till": till_date,
        "source": "moex_iss_candles_rate",
        **meta,
        "rows": normalized,
    }


def _read_ruonia_xlsx(raw: bytes) -> list[tuple[Any, Any]]:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return []
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True) if sheet is not None else iter(())
        header = [str(value).strip() if value is not None else "" for value in next(rows, ())]
        if "DT" not in header or "ruo" not in header:
            return []
        date_index, rate_index = header.index("DT"), header.index("ruo")
        width = max(date_index, rate_index) + 1
        return [
            (row[date_index], row[rate_index])
            for row in rows
            if len(row) >= width and row[date_index] is not None and row[rate_index] is not None
        ]
    finally:
        workbook.close()


def _parse_ruonia_pair(
    date_text: Any, rate_text: Any, start: date, finish: date
) -> dict[str, Any] | None:
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
    if not start <= row_date <= finish:
        return None
    return {
        "date": row_date.isoformat(),
        "rate": rate,
        "source_ticker": "CBR_RUONIA",
        "source_kind": "synthetic_proxy",
    }


def _fetch_ruonia_chunk(start: date, end: date) -> tuple[list[tuple[Any, Any]], list[str], str]:
    """The HTML dynamics table first, then the XLSX export the current CBR site serves."""
    query = urlencode(
        {
            "UniDbQuery.Posted": "True",
            "UniDbQuery.From": f"{start:%d.%m.%Y}",
            "UniDbQuery.To": f"{end:%d.%m.%Y}",
        }
    )
    raw, html_hash, _ = _fetch_or_empty(
        f"{CBR_BASE}/hd_base/ruonia/dynamics/?{query}", "text/html,application/xhtml+xml"
    )
    pairs = RUONIA_HTML_PATTERN.findall(raw.decode("utf-8", errors="replace"))
    if pairs:
        return pairs, [html_hash], "html"
    query = urlencode(
        {
            "FromDate": f"{start:%m/%d/%Y}",
            "ToDate": f"{end:%m/%d/%Y}",
            "posted": "False",
            "backUrl": "/hd_base/ruonia/dynamics/",
        }
    )
    raw, xlsx_hash, content_type = _fetch_or_empty(
        f"{CBR_BASE}/Queries/UniDbQuery/DownloadExcel/14315?{query}",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
    )
    pairs = _read_ruonia_xlsx(raw) if "spreadsheet" in content_type.lower() else []
    return pairs, [html_hash, xlsx_hash], "xlsx" if pairs else "unavailable"


def fetch_cbr_ruonia_rates(
    from_date: str, till_date: str, *, delay_seconds: float = 0.2
) -> dict[str, Any]:
    """Fetch RUONIA in bounded yearly CBR requests; an unavailable source stays empty."""
    start, finish = date.fromisoformat(from_date), date.fromisoformat(till_date)
    rows: dict[str, dict[str, Any]] = {}
    hashes: list[str] = []
    methods: list[str] = []
    cursor = start
    while cursor <= finish:
        chunk_end = min(cursor + timedelta(days=364), finish)
        pairs, chunk_hashes, method = _fetch_ruonia_chunk(cursor, chunk_end)
        hashes.extend(chunk_hashes)
        methods.append(method)
        for date_text, rate_text in pairs:
            if row := _parse_ruonia_pair(date_text, rate_text, start, finish):
                rows[row["date"]] = row
        cursor = chunk_end + timedelta(days=1)
        if cursor <= finish and delay_seconds:
            time.sleep(delay_seconds)
    normalized = [rows[key] for key in sorted(rows)]
    return {
        "ticker": "CBR_RUONIA",
        "from": from_date,
        "till": till_date,
        "source": "cbr_ruonia_html_or_xlsx",
        "request_count": len(hashes),
        "methods": methods,
        "raw_response_hashes": hashes,
        "status": "available" if normalized else "unavailable_or_unparsed",
        "rows": normalized,
    }


__all__ = [
    "BENCHMARK_SCHEMA",
    "CHAIN",
    "OBSERVED",
    "BenchmarkError",
    "adjust_vtbm_split",
    "build_lineage",
    "fetch_cbr_ruonia_rates",
    "fetch_moex_history",
    "fetch_rusfar_rates",
    "rate_rows_to_price",
]
