"""Hourly (H1) candle reading and network/local fallback helpers for calendar spreads."""

from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.request
from bisect import bisect_left
from datetime import datetime
from pathlib import Path
from typing import Any

from moex_spread_lab.data.lake.data_lake import BarSeries, DataLakeError, MarketDataStore

from .contracts import (
    MIN_H1_CALENDAR_OBSERVATIONS,
    MIN_H1_NATIVE_SESSION_VOLUME,
    MIN_H1_PROXY_LEG_SESSION_VOLUME,
)


def candle_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) < 16:
        return None
    timestamp = value[:19].replace(" ", "T")
    try:
        datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return timestamp


def finite_num(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def asof_before(
    days: list[str], values: dict[str, float], day: str
) -> tuple[str | None, float | None]:
    index = bisect_left(days, day) - 1
    if index < 0:
        return None, None
    key = days[index]
    return key, values.get(key)


def _parse_candle_rows(
    raw_rows: list[dict[str, Any]], *, non_negative_vol: bool = False
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for source_row in raw_rows:
        ts = candle_timestamp(source_row.get("begin"))
        close = finite_num(source_row.get("close"))
        if ts is None or close is None:
            continue
        vol = finite_num(source_row.get("volume"))
        rows[ts] = {
            "begin": ts,
            "open": finite_num(source_row.get("open")),
            "close": close,
            "volume": (vol if vol is not None and vol >= 0 else None) if non_negative_vol else vol,
        }
    return rows


def _read_local_h1_fallback(
    target: Path,
    secid: str,
    cache: dict[str, tuple[tuple[Any, ...], tuple[dict[str, Any] | None, str | None]]],
) -> tuple[dict[str, Any] | None, str | None] | None:
    if not target.exists():
        return None
    try:
        st = target.stat()
        fp = ("local_1h", str(target), st.st_mtime_ns, st.st_size)
        cached = cache.get(secid)
        if cached and cached[0] == fp:
            return cached[1]
        raw = target.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        rows = _parse_candle_rows(payload.get("rows", []))
        if not rows:
            return None
        res: tuple[dict[str, Any] | None, str | None] = (
            {
                "secid": secid,
                "path": str(target),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "rows": rows,
            },
            None,
        )
        cache[secid] = (fp, res)
        return res
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _download_h1_candles(target: Path, secid: str) -> tuple[dict[str, Any] | None, str | None]:
    url = (
        f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}/"
        "candles.json?interval=60&from=2026-06-01&iss.meta=off"
    )
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        candles = payload.get("candles", {})
        cols, data = candles.get("columns", []), candles.get("data", [])
        if not cols or not data:
            return None, "no_local_h1_file"
        raw_rows = [dict(zip(cols, row, strict=False)) for row in data]
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            {"schema_version": "iss-candles.v1", "secid": secid, "interval": 60, "rows": raw_rows},
            ensure_ascii=False,
        ).encode("utf-8")
        target.write_bytes(encoded)
        rows = _parse_candle_rows(raw_rows)
        if not rows:
            return None, "no_valid_h1_rows"
        return (
            {
                "secid": secid,
                "path": str(target),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "rows": rows,
            },
            None,
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return None, "no_local_h1_file"


def fetch_h1_candles(
    root: Path,
    secid: str,
    cache: dict[str, tuple[tuple[Any, ...], tuple[dict[str, Any] | None, str | None]]],
    attempted: set[str],
) -> tuple[dict[str, Any] | None, str | None]:
    target = root / "data/w1/candles/1h" / f"{secid}_1h.json"
    local_res = _read_local_h1_fallback(target, secid, cache)
    if local_res is not None:
        return local_res
    offline = os.environ.get("MOEX_OFFLINE_ONLY") == "1" or "PYTEST_CURRENT_TEST" in os.environ
    if offline or secid in attempted:
        return None, "no_local_h1_file"
    attempted.add(secid)
    return _download_h1_candles(target, secid)


def _decode_bar_series(source: BarSeries, secid: str) -> tuple[dict[str, Any] | None, str | None]:
    if source.schema_version != "iss-candles.v1" or source.interval != 60:
        return None, "invalid_h1_schema_or_interval"
    if source.secid not in (None, secid):
        return None, "secid_mismatch"
    if not source.rows:
        return None, "empty_h1_series"
    rows = _parse_candle_rows(list(source.rows), non_negative_vol=True)
    if not rows:
        return None, "no_valid_h1_rows"
    return (
        {
            "secid": secid,
            "path": source.source_path,
            "sha256": source.source_sha256,
            "rows": rows,
        },
        None,
    )


def read_h1_candles(
    root: Path,
    bar_store: MarketDataStore,
    secid: str,
    cache: dict[str, tuple[tuple[Any, ...], tuple[dict[str, Any] | None, str | None]]],
    attempted: set[str],
    *,
    allow_fetch: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    path = bar_store.select_h1(secid)
    if not bar_store.source_available(path):
        if allow_fetch or (root / "data/w1/candles/1h" / f"{secid}_1h.json").exists():
            return fetch_h1_candles(root, secid, cache, attempted)
        return None, "no_local_h1_file"
    try:
        source = bar_store.read(path, columns=("begin", "open", "close", "volume"))
        fingerprint = source.cache_key
        cached = cache.get(secid)
        if cached and cached[0] == fingerprint:
            return cached[1]
        result = _decode_bar_series(source, secid)
        cache[secid] = (fingerprint, result)
        return result
    except (DataLakeError, OSError, TypeError, ValueError):
        return None, "unreadable_h1_series"


def select_h1_source_mode(
    native_common: list[str], native_source: dict[str, Any] | None, native_error: str | None
) -> tuple[bool, dict[str, dict[str, Any]], str | None, float, str, str, str]:
    if len(native_common) >= MIN_H1_CALENDAR_OBSERVATIONS and native_source is not None:
        return (
            True,
            native_source["rows"],
            None,
            MIN_H1_NATIVE_SESSION_VOLUME,
            "native_spread_session_volume",
            "минимум дневного объёма нативного спреда",
            "Сумма объёма свечей нативного календарного спреда за сессию; каждый часовой бар исполним только при достаточном объёме.",
        )
    fallback = native_error or (
        "native_h1_insufficient_overlap" if native_source else "native_h1_unavailable"
    )
    return (
        False,
        {},
        fallback,
        MIN_H1_PROXY_LEG_SESSION_VOLUME,
        "matched_near_far_session_volume",
        "минимум суммарных объёмов синхронных баров обеих ног за сессию",
        "Прокси far − near: минимум суммарных объёмов ближней и дальней ноги на синхронных часовых отметках; бар исполним при объёме обеих ног.",
    )
