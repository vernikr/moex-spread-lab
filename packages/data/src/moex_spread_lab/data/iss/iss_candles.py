"""MOEX ISS paginated candle series download."""

from __future__ import annotations

import json
import math
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .iss_http import (
    CANDLE_FIELDS,
    CANDLE_INTERVAL_LABELS,
    CANDLE_PAGE_SIZE,
    DEFAULT_BOARD,
    DEFAULT_ISS_BASE_URL,
    IssIntakeError,
    fetch_json_with_raw,
)
from .iss_http import (
    candles_url as _candles_url,
)
from .iss_http import (
    rows as _rows,
)

RawSinkCallback = Callable[[str, bytes, str, str], None]


@dataclass(frozen=True)
class _CandleSeriesRequest:
    secid: str
    interval: int
    from_date: str
    till_date: str
    base_url: str
    board: str
    timeout: float
    delay_seconds: float
    cache_dir: Path | None
    max_pages: int | None
    retries: int
    opener: Callable[..., Any]
    raw_sink: RawSinkCallback | None


def _valid_numeric(value: Any) -> bool:
    return value is None or (
        type(value) is int or (isinstance(value, float) and math.isfinite(value))
    )


def normalize_candles(
    payload: dict[str, Any],
    *,
    secid: str,
    interval: int,
    url: str,
    received_at: str,
    raw_sha256: str,
) -> list[dict[str, Any]]:
    rows = _rows(payload.get("candles"), "candles")
    columns = set(payload.get("candles", {}).get("columns", []))
    missing = set(CANDLE_FIELDS) - columns
    if missing:
        raise IssIntakeError(f"candle fields missing for {secid}: {sorted(missing)}")
    normalized: list[dict[str, Any]] = []
    for row in rows:
        begin = row.get("begin")
        end = row.get("end")
        if not isinstance(begin, str) or not isinstance(end, str):
            raise IssIntakeError(f"candle timestamps missing for {secid}")
        close = row.get("close")
        if close is None:
            raise IssIntakeError(f"candle close missing for {secid}")
        for field in ("open", "close", "high", "low", "value", "volume"):
            value = row.get(field)
            if not _valid_numeric(value):
                raise IssIntakeError(f"invalid candle {field} for {secid}")
        normalized.append(
            {
                "secid": secid,
                "interval": interval,
                "timeframe": CANDLE_INTERVAL_LABELS.get(interval, f"interval_{interval}"),
                "begin": begin,
                "end": end,
                "open": row.get("open"),
                "close": close,
                "high": row.get("high"),
                "low": row.get("low"),
                "value": row.get("value"),
                "volume": row.get("volume"),
                "price_proxy": close,
                "price_proxy_kind": "candle_close",
                "price_proxy_assumption": "user_A-06_candle_is_usable_medium_term_price_proxy",
                "source": {
                    "url": url,
                    "received_at": received_at,
                    "raw_response_sha256": raw_sha256,
                },
            }
        )
    return normalized


def _try_load_cached_page(cache_path: Path | None, url: str) -> dict[str, Any] | None:
    if cache_path is None or not cache_path.exists():
        return None
    try:
        cached: object = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(cached, dict):
        page = cast(dict[str, Any], cached)
        if page.get("url") == url and isinstance(page.get("rows"), list):
            return page
    return None


def _fetch_network_candle_page(
    req: _CandleSeriesRequest, url: str
) -> tuple[dict[str, Any], str, str]:
    if req.delay_seconds:
        time.sleep(req.delay_seconds)
    last_error: IssIntakeError | None = None
    for attempt in range(req.retries + 1):
        try:
            payload, received_at, raw_sha256, raw = fetch_json_with_raw(
                url, timeout=req.timeout, opener=req.opener
            )
            if req.raw_sink is not None:
                req.raw_sink(url, raw, received_at, raw_sha256)
            return payload, received_at, raw_sha256
        except IssIntakeError as exc:
            last_error = exc
            if attempt < req.retries:
                time.sleep(min(2.0, 0.25 * (attempt + 1)))
    assert last_error is not None
    raise last_error


def _load_or_fetch_page_rows(
    req: _CandleSeriesRequest, series_cache: Path | None, page: int, start: int
) -> list[dict[str, Any]]:
    url = _candles_url(
        req.base_url, req.board, req.secid, req.interval, req.from_date, req.till_date, start
    )
    cache_path = series_cache / f"page-{page:06d}.json" if series_cache else None
    page_result = _try_load_cached_page(cache_path, url)
    if page_result is None:
        payload, received_at, raw_sha256 = _fetch_network_candle_page(req, url)
        rows = normalize_candles(
            payload,
            secid=req.secid,
            interval=req.interval,
            url=url,
            received_at=received_at,
            raw_sha256=raw_sha256,
        )
        page_result = {"url": url, "page": page, "start": start, "rows": rows}
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(page_result, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
    rows = cast(list[dict[str, Any]], page_result["rows"])
    if req.interval == 4:
        return [{**row, "timeframe": CANDLE_INTERVAL_LABELS[4]} for row in rows]
    return rows


def _paginate_candle_rows(req: _CandleSeriesRequest) -> tuple[list[dict[str, Any]], int]:
    series_cache = req.cache_dir / req.secid / str(req.interval) if req.cache_dir else None
    all_rows: list[dict[str, Any]] = []
    page = 0
    while True:
        start = page * CANDLE_PAGE_SIZE
        rows = _load_or_fetch_page_rows(req, series_cache, page, start)
        if rows and all_rows and rows[0]["begin"] <= all_rows[-1]["begin"]:
            raise IssIntakeError(
                f"candle pagination did not advance: {req.secid} interval={req.interval} start={start}"
            )
        all_rows.extend(rows)
        page += 1
        if (
            not rows
            or len(rows) < CANDLE_PAGE_SIZE
            or (req.max_pages is not None and page >= req.max_pages)
        ):
            break
    return all_rows, page


def fetch_candle_series(  # noqa: PLR0913 - explicit keyword arguments preserve the public API
    *,
    secid: str,
    interval: int,
    from_date: str,
    till_date: str,
    base_url: str = DEFAULT_ISS_BASE_URL,
    board: str = DEFAULT_BOARD,
    timeout: float = 20.0,
    delay_seconds: float = 0.1,
    cache_dir: Path | None = None,
    max_pages: int | None = None,
    retries: int = 3,
    opener: Callable[..., Any] = urllib.request.urlopen,
    raw_sink: RawSinkCallback | None = None,
) -> dict[str, Any]:
    if interval <= 0 or delay_seconds < 0 or retries < 0:
        raise IssIntakeError("invalid candle interval, delay or retries")
    req = _CandleSeriesRequest(
        secid,
        interval,
        from_date,
        till_date,
        base_url,
        board,
        timeout,
        delay_seconds,
        cache_dir,
        max_pages,
        retries,
        opener,
        raw_sink,
    )
    all_rows, page = _paginate_candle_rows(req)
    deduped = {str(row["begin"]): row for row in all_rows}
    rows = [deduped[key] for key in sorted(deduped)]
    return {
        "schema_version": "iss-candles.v1",
        "secid": secid,
        "board": board,
        "interval": interval,
        "timeframe": CANDLE_INTERVAL_LABELS.get(interval, f"interval_{interval}"),
        "from": from_date,
        "till": till_date,
        "page_count": page,
        "raw_row_count": len(all_rows),
        "row_count": len(rows),
        "duplicate_count": len(all_rows) - len(rows),
        "coverage_status": "available" if rows else "empty",
        "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
        "rows": rows,
    }
