"""BarSeries normalization and benchmark alignment for pair screening."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Collection
from functools import partial
from pathlib import Path
from typing import Any

from moex_spread_lab.core.timeframes import get_timeframe, timeframe_for_interval
from moex_spread_lab.core.w0 import W0Error, read_hashed_json
from moex_spread_lab.data.lake.data_lake import BarSeries, DataLakeError, LegacyJsonBarStore

from ..pair_mean_reversion.analytics import load_contracts

# Preserve pre-leaf wildcard bindings as well as the declared screener hooks.
__all__ = [
    "Any",
    "BarSeries",
    "Collection",
    "DataLakeError",
    "LegacyJsonBarStore",
    "Path",
    "ScreenError",
    "W0Error",
    "_bar_series",
    "_normalized_series",
    "_read_json",
    "_round",
    "annotations",
    "bisect_left",
    "get_timeframe",
    "load_contracts",
    "partial",
    "read_hashed_json",
    "timeframe_for_interval",
]

_NORM_EXTRA_OPTS = frozenset({"prior_benchmark_dates", "timeframe"})
_NORMALIZED_BY_TS_CACHE: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}


class ScreenError(W0Error):
    """Raised when the offline pair screen cannot be built."""


def _round(value: float) -> float:
    return round(float(value), 8)


_read_json = partial(read_hashed_json, error=ScreenError, label="screen input")


def _path_source(value: object) -> Path | None:
    """Return ``value`` when it is a filesystem path; callers may pass other payloads."""
    return value if isinstance(value, Path) else None


def _bar_series(source: BarSeries | Path) -> BarSeries:
    if isinstance(source, BarSeries):
        return source
    path = _path_source(source)
    if path is None:
        raise ScreenError("screen candle source must be a BarSeries or Path")
    try:
        return LegacyJsonBarStore().read(path)
    except DataLakeError as exc:
        raise ScreenError(str(exc)) from exc


def _canonical_timestamp(row: dict[str, Any]) -> str | None:
    value = row.get("begin")
    if not isinstance(value, str) or len(value) < 10:
        return None
    return value[:19].replace(" ", "T") if len(value) >= 19 else value[:10]


def _rows_by_ts(series: BarSeries) -> dict[str, dict[str, Any]]:
    cached = _NORMALIZED_BY_TS_CACHE.get(series.cache_key)
    if cached is not None:
        return cached
    mapped = {
        ts: row
        for row in series.rows
        if (ts := _canonical_timestamp(row)) and row.get("close") is not None
    }
    if len(_NORMALIZED_BY_TS_CACHE) > 1024:
        _NORMALIZED_BY_TS_CACHE.clear()
    _NORMALIZED_BY_TS_CACHE[series.cache_key] = mapped
    return mapped


def _resolve_benchmark_mark(
    session_date: str,
    intraday: bool,
    benchmark: dict[str, dict[str, Any]],
    observed: list[str],
    prior_dates: Collection[str],
) -> tuple[dict[str, Any] | None, str | None, str]:
    row = benchmark.get(session_date)
    if intraday or (row is None and session_date in prior_dates):
        idx = bisect_left(observed, session_date) - 1
        b_date = observed[idx] if idx >= 0 else None
        return (benchmark.get(b_date) if b_date else None), b_date, "prior_daily_close"
    return row, session_date, "daily_close_same_date"


def _build_normalized_rows(
    near_rows: dict[str, dict[str, Any]],
    far_rows: dict[str, dict[str, Any]],
    benchmark: dict[str, dict[str, Any]],
    *,
    intraday: bool,
    prior_dates: Collection[str],
    multipliers: tuple[float, float],
    common_tick_rub: float,
) -> list[dict[str, Any]]:
    near_mult, far_mult = multipliers
    observed = sorted(benchmark)
    rows: list[dict[str, Any]] = []
    for timestamp in sorted(set(near_rows) & set(far_rows)):
        session_date = timestamp[:10]
        b_row, b_date, mark_kind = _resolve_benchmark_mark(
            session_date, intraday, benchmark, observed, prior_dates
        )
        if b_row is None:
            continue
        near_row, far_row = near_rows[timestamp], far_rows[timestamp]
        near_close, far_close = float(near_row["close"]), float(far_row["close"])
        norm_spread = near_close * near_mult - far_close * far_mult
        rows.append(
            {
                "date": timestamp if intraday else session_date,
                "session_date": session_date,
                "timestamp": timestamp,
                "near_close": near_close,
                "far_close": far_close,
                "near_open": float(near_row.get("open") or near_close),
                "far_open": float(far_row.get("open") or far_close),
                "spread_value": _round(norm_spread),
                "spread_ticks": _round(norm_spread / common_tick_rub),
                "benchmark_price": float(b_row["price"]),
                "benchmark_source_ticker": b_row.get("source_ticker"),
                "benchmark_source_kind": b_row.get("source_kind"),
                "benchmark_mark_kind": mark_kind,
                "benchmark_mark_date": b_date,
                "near_price_proxy_kind": near_row.get("price_proxy_kind", "candle_close"),
                "far_price_proxy_kind": far_row.get("price_proxy_kind", "candle_close"),
                "near_volume": near_row.get("volume"),
                "far_volume": far_row.get("volume"),
            }
        )
    return rows


def _normalized_series(
    near_source: BarSeries | Path,
    far_source: BarSeries | Path,
    benchmark_path: Path,
    contracts_path: Path,
    *,
    near: str,
    far: str,
    **options: Any,
) -> dict[str, Any]:
    unknown = set(options) - _NORM_EXTRA_OPTS
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {sorted(unknown)}")
    prior_dates: Collection[str] = options.get("prior_benchmark_dates", ())
    timeframe: str | None = options.get("timeframe")
    near_series = _bar_series(near_source)
    far_series = _bar_series(far_source)
    benchmark_payload, benchmark_hash = _read_json(benchmark_path)
    if (
        near_series.schema_version != "iss-candles.v1"
        or far_series.schema_version != "iss-candles.v1"
    ):
        raise ScreenError("screen requires iss-candles.v1 pair inputs")
    if benchmark_payload.get("schema_version") != "lqdt-lineage.v1":
        raise ScreenError("screen requires lqdt-lineage.v1 benchmark")
    try:
        spec = (
            get_timeframe(timeframe)
            if timeframe is not None
            else timeframe_for_interval(near_series.interval)
        )
    except ValueError as exc:
        raise ScreenError(str(exc)) from exc
    if near_series.interval != spec.iss_interval or far_series.interval != spec.iss_interval:
        raise ScreenError(f"candle interval mismatch: expected {spec.iss_interval} ({spec.id})")
    contracts, contracts_hash = load_contracts(contracts_path, near, far)
    near_c, far_c = contracts[near], contracts[far]
    near_mult = float(near_c["tick_value"]) / float(near_c["tick_size"])
    far_mult = float(far_c["tick_value"]) / float(far_c["tick_size"])
    common_tick_rub = max(float(near_c["tick_value"]), float(far_c["tick_value"]), 1.0)
    benchmark = {row["date"]: row for row in benchmark_payload.get("rows", []) if row.get("date")}
    rows = _build_normalized_rows(
        _rows_by_ts(near_series),
        _rows_by_ts(far_series),
        benchmark,
        intraday=spec.intraday,
        prior_dates=prior_dates,
        multipliers=(near_mult, far_mult),
        common_tick_rub=common_tick_rub,
    )
    if len(rows) < 2:
        raise ScreenError(f"fewer than two normalized common rows for {near}/{far}")
    return {
        "schema_version": "analytical-series.v1",
        "near": near,
        "far": far,
        "interval": spec.iss_interval,
        "timeframe": spec.id,
        "direction": "near_minus_far_normalized_rub",
        "price_unit": "rub_normalized_per_pair",
        "spread_tick_value_rub": common_tick_rub,
        "rows": rows,
        "contracts": contracts,
        "provenance": {
            "near_candles_sha256": near_series.source_sha256,
            "far_candles_sha256": far_series.source_sha256,
            "benchmark_sha256": benchmark_hash,
            "contracts_sha256": contracts_hash,
            "near_candles_path": near_series.source_path,
            "far_candles_path": far_series.source_path,
            "benchmark_path": str(benchmark_path),
            "contracts_path": str(contracts_path),
            "normalization": "close * (tick_value / tick_size) per leg",
            "benchmark_alignment": "previous observed daily close for intraday bars"
            if spec.intraday
            else "same-day close; documented non-clearing fallback only",
        },
    }
