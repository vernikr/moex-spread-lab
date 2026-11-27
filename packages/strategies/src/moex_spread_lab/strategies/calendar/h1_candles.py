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

from moex_spread_lab.data import DataLakeError, MarketDataStore
from moex_spread_lab.data.lake import BarSeries

from ..calendar_eval import row_dict
from .contracts import (
    MIN_H1_CALENDAR_OBSERVATIONS,
    MIN_H1_NATIVE_SESSION_VOLUME,
    MIN_H1_PROXY_LEG_SESSION_VOLUME,
)

_CANDLE_KEYS = "begin open close volume"
_SOURCE_KEYS = "secid path sha256 rows"
_DOWNLOAD_KEYS = "schema_version secid interval rows"


def candle_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) < 16:
        return None
    a = value[:19].replace(" ", "T")
    try:
        datetime.fromisoformat(a)
    except ValueError:
        return None
    return a


def finite_num(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    a = float(value)
    return a if math.isfinite(a) else None


def asof_before(
    days: list[str], values: dict[str, float], day: str
) -> tuple[str | None, float | None]:
    a = bisect_left(days, day) - 1
    if a < 0:
        return None, None
    b = days[a]
    return b, values.get(b)


def _parse_candle_rows(
    raw_rows: list[dict[str, Any]], *, non_negative_vol: bool = False
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for a in raw_rows:
        c = candle_timestamp(a.get("begin"))
        d = finite_num(a.get("close"))
        if c is None or d is None:
            continue
        b = finite_num(a.get("volume"))
        rows[c] = row_dict(
            _CANDLE_KEYS,
            (
                c,
                finite_num(a.get("open")),
                d,
                (b if b is not None and b >= 0 else None) if non_negative_vol else b,
            ),
        )
    return rows


def _h1_source(
    secid: str, path: str, digest: str, rows: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    return row_dict(_SOURCE_KEYS, (secid, path, digest, rows)), None


def _read_local_h1_fallback(
    target: Path,
    secid: str,
    cache: dict[str, tuple[tuple[Any, ...], tuple[dict[str, Any] | None, str | None]]],
) -> tuple[dict[str, Any] | None, str | None] | None:
    if not target.exists():
        return None
    try:
        b = target.stat()
        c = "local_1h", str(target), b.st_mtime_ns, b.st_size
        a = cache.get(secid)
        if a and a[0] == c:
            return a[1]
        d = target.read_bytes()
        rows = _parse_candle_rows(json.loads(d.decode("utf-8")).get("rows", []))
        if not rows:
            return None
        e = _h1_source(secid, str(target), hashlib.sha256(d).hexdigest(), rows)
        cache[secid] = c, e
        return e
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _download_h1_candles(target: Path, secid: str) -> tuple[dict[str, Any] | None, str | None]:
    e = f"https://iss.moex.com/iss/engines/futures/markets/forts/securities/{secid}/candles.json?interval=60&from=2026-06-01&iss.meta=off"
    try:
        with urllib.request.urlopen(e, timeout=5) as f:
            g = json.loads(f.read().decode("utf-8"))
        a = g.get("candles", {})
        b, c = a.get("columns", []), a.get("data", [])
        if not b or not c:
            return None, "no_local_h1_file"
        raw_rows = [dict(zip(b, a, strict=False)) for a in c]
        target.parent.mkdir(parents=True, exist_ok=True)
        d = json.dumps(
            row_dict(_DOWNLOAD_KEYS, ("iss-candles.v1", secid, 60, raw_rows)), ensure_ascii=False
        ).encode("utf-8")
        target.write_bytes(d)
        rows = _parse_candle_rows(raw_rows)
        if not rows:
            return None, "no_valid_h1_rows"
        return _h1_source(secid, str(target), hashlib.sha256(d).hexdigest(), rows)
    except (OSError, ValueError, TypeError, AttributeError):
        return None, "no_local_h1_file"


def fetch_h1_candles(
    root: Path,
    secid: str,
    cache: dict[str, tuple[tuple[Any, ...], tuple[dict[str, Any] | None, str | None]]],
    attempted: set[str],
    *,
    offline: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    target = root / "data/w1/candles/1h" / f"{secid}_1h.json"
    a = _read_local_h1_fallback(target, secid, cache)
    if a is not None:
        return a
    offline = (
        offline or os.environ.get("MOEX_OFFLINE_ONLY") == "1" or "PYTEST_CURRENT_TEST" in os.environ
    )
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
    return _h1_source(secid, source.source_path, source.source_sha256, rows)


def read_h1_candles(
    root: Path,
    bar_store: MarketDataStore,
    secid: str,
    cache: dict[str, tuple[tuple[Any, ...], tuple[dict[str, Any] | None, str | None]]],
    attempted: set[str],
    *,
    allow_fetch: bool = True,
    offline: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    path = bar_store.select_h1(secid)
    if not bar_store.source_available(path):
        if allow_fetch or (root / "data/w1/candles/1h" / f"{secid}_1h.json").exists():
            return fetch_h1_candles(root, secid, cache, attempted, offline=offline)
        return None, "no_local_h1_file"
    try:
        source = bar_store.read(path, columns=("begin", "open", "close", "volume"))
        b = source.cache_key
        a = cache.get(secid)
        if a and a[0] == b:
            return a[1]
        c = _decode_bar_series(source, secid)
        cache[secid] = b, c
        return c
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
    a = native_error or (
        "native_h1_insufficient_overlap" if native_source else "native_h1_unavailable"
    )
    return (
        False,
        {},
        a,
        MIN_H1_PROXY_LEG_SESSION_VOLUME,
        "matched_near_far_session_volume",
        "минимум суммарных объёмов синхронных баров обеих ног за сессию",
        "Прокси far − near: минимум суммарных объёмов ближней и дальней ноги на синхронных часовых отметках; бар исполним при объёме обеих ног.",
    )
