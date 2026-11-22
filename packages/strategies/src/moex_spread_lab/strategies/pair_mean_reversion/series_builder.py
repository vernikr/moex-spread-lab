"""Input loaders and series builders for analytical pair baselines."""

from __future__ import annotations

from bisect import bisect_left
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import load_contracts as _load_w0_contracts
from moex_spread_lab.core.w0 import read_hashed_json

from .costs_helper import (
    ANALYTICS_SCHEMA,
    COST_SCENARIOS,
    REQUIRED_COST_COMPONENTS,
    AnalyticsError,
    _calendar_days,
    _number,
    _round,
    _trade_cost_breakdown,
    _validate_cost_profile,
    get_cost_scenario,
)


def _date_key(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) < 10:
        raise AnalyticsError(f"{field} must contain an ISO date/timestamp")
    return value[:10]


def load_contracts(path: Path, near: str, far: str) -> tuple[dict[str, Any], str]:
    return _load_w0_contracts(path, near, far, error=AnalyticsError)


def _parse_candle_row(secid: str, row: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(row, dict):
        raise AnalyticsError(f"malformed candle row for {secid}")
    raw_ts = row.get("begin")
    date_key = _date_key(raw_ts, f"{secid}.begin")
    if not isinstance(raw_ts, str) or len(raw_ts) < 19:
        raise AnalyticsError(f"{secid}.begin must contain a timestamp")
    timestamp = raw_ts[:19]
    close = _number(row.get("close"), f"{secid}.{timestamp}.close", positive=True)
    if row.get("price_proxy_kind") != "candle_close":
        raise AnalyticsError(f"candle row does not declare close proxy: {secid} {date_key}")
    raw_open = row.get("open")
    open_px = (
        float(raw_open)
        if not isinstance(raw_open, bool) and isinstance(raw_open, (int, float)) and raw_open > 0
        else close
    )
    return timestamp, {
        "date": date_key,
        "timestamp": timestamp,
        "open": open_px,
        "close": close,
        "source": row.get("source"),
        "price_proxy_kind": row.get("price_proxy_kind"),
    }


def _load_candles(path: Path, secid: str, interval: int) -> tuple[dict[str, dict[str, Any]], str]:
    payload, digest = read_hashed_json(path, error=AnalyticsError)
    if payload.get("schema_version") != "iss-candles.v1" or payload.get("secid") != secid:
        raise AnalyticsError(f"unsupported candle input for {secid}: {path}")
    if payload.get("interval") != interval:
        raise AnalyticsError(f"candle interval mismatch for {secid}: expected {interval}")
    result = dict(_parse_candle_row(secid, r) for r in payload.get("rows", []))
    if not result:
        raise AnalyticsError(f"empty candle input for {secid}")
    return result, digest


def _load_benchmark(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    payload, digest = read_hashed_json(path, error=AnalyticsError)
    if payload.get("schema_version") != "lqdt-lineage.v1":
        raise AnalyticsError("unsupported LQDT benchmark schema")
    result: dict[str, dict[str, Any]] = {}
    for row in payload.get("rows", []):
        if not isinstance(row, dict):
            raise AnalyticsError("malformed LQDT benchmark row")
        date_key = _date_key(row.get("date"), "benchmark.date")
        result[date_key] = {
            "date": date_key,
            "price": _number(row.get("price"), f"benchmark.{date_key}.price", positive=True),
            "source_ticker": row.get("source_ticker"),
            "source_kind": row.get("source_kind"),
        }
    if not result:
        raise AnalyticsError("empty LQDT benchmark")
    return result, digest


def _select_benchmark_mark(
    benchmark: dict[str, dict[str, Any]],
    benchmark_dates: list[str],
    last_timestamp_by_date: dict[str, str],
    interval: int,
    timestamp: str,
    date_key: str,
) -> tuple[dict[str, Any] | None, str]:
    if interval == 24 or last_timestamp_by_date[date_key] == timestamp:
        return benchmark.get(date_key), "daily_close_same_date"
    prior_index = bisect_left(benchmark_dates, date_key) - 1
    row = benchmark.get(benchmark_dates[prior_index]) if prior_index >= 0 else None
    return row, "prior_daily_close"


def build_series(
    near_path: Path,
    far_path: Path,
    benchmark_path: Path,
    contracts_path: Path,
    *,
    near: str = "CNYRUBF",
    far: str = "CRZ6",
    interval: int = 24,
) -> dict[str, Any]:
    near_rows, near_hash = _load_candles(near_path, near, interval)
    far_rows, far_hash = _load_candles(far_path, far, interval)
    benchmark, benchmark_hash = _load_benchmark(benchmark_path)
    contracts, contracts_hash = load_contracts(contracts_path, near, far)
    common_ts = sorted(set(near_rows) & set(far_rows))
    if len(common_ts) < 2:
        raise AnalyticsError("fewer than two common pair timestamps")
    last_ts_by_date = {near_rows[ts]["date"]: ts for ts in common_ts}
    b_dates = sorted(benchmark)
    if contracts[near]["tick_size"] != contracts[far]["tick_size"]:
        raise AnalyticsError("baseline requires equal tick sizes")
    tick_size = float(contracts[near]["tick_size"])
    rows: list[dict[str, Any]] = []
    for ts in common_ts:
        date_key = near_rows[ts]["date"]
        b_row, mark_kind = _select_benchmark_mark(
            benchmark, b_dates, last_ts_by_date, interval, ts, date_key
        )
        if b_row is None:
            continue
        near_close, far_close = float(near_rows[ts]["close"]), float(far_rows[ts]["close"])
        sp_val = near_close - far_close
        rows.append(
            {
                "date": date_key,
                "timestamp": ts,
                "near_close": near_close,
                "far_close": far_close,
                "near_open": near_rows[ts]["open"],
                "far_open": far_rows[ts]["open"],
                "spread_value": _round(sp_val),
                "spread_ticks": _round(sp_val / tick_size),
                "benchmark_price": b_row["price"],
                "benchmark_source_ticker": b_row["source_ticker"],
                "benchmark_source_kind": b_row["source_kind"],
                "benchmark_mark_kind": mark_kind,
                "near_price_proxy_kind": near_rows[ts]["price_proxy_kind"],
                "far_price_proxy_kind": far_rows[ts]["price_proxy_kind"],
            }
        )
    if len(rows) < 2:
        raise AnalyticsError("fewer than two pair timestamps with benchmark marks")
    return {
        "schema_version": "analytical-series.v1",
        "near": near,
        "far": far,
        "interval": interval,
        "timeframe": {1: "1m", 10: "10m", 60: "1h", 24: "1d"}.get(interval, f"interval_{interval}"),
        "direction": "near_minus_far",
        "rows": rows,
        "contracts": contracts,
        "provenance": {
            "near_candles_sha256": near_hash,
            "far_candles_sha256": far_hash,
            "benchmark_sha256": benchmark_hash,
            "contracts_sha256": contracts_hash,
            "near_candles_path": str(near_path),
            "far_candles_path": str(far_path),
            "benchmark_path": str(benchmark_path),
            "contracts_path": str(contracts_path),
        },
    }


def build_daily_series(
    near_path: Path,
    far_path: Path,
    benchmark_path: Path,
    contracts_path: Path,
    *,
    near: str = "CNYRUBF",
    far: str = "CRZ6",
) -> dict[str, Any]:
    return build_series(
        near_path, far_path, benchmark_path, contracts_path, near=near, far=far, interval=24
    )


__all__ = [
    "ANALYTICS_SCHEMA",
    "COST_SCENARIOS",
    "REQUIRED_COST_COMPONENTS",
    "AnalyticsError",
    "_calendar_days",
    "_date_key",
    "_load_benchmark",
    "_load_candles",
    "_number",
    "_round",
    "_trade_cost_breakdown",
    "_validate_cost_profile",
    "build_daily_series",
    "build_series",
    "get_cost_scenario",
    "load_contracts",
]
