"""Candle inputs, cost assumptions, and series builders for pair baselines."""

from __future__ import annotations

import math
from bisect import bisect_left
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.accounting.costs import COST_COMPONENTS, COST_SCHEMA
from moex_spread_lab.core.w0 import W0Error, read_hashed_json
from moex_spread_lab.core.w0 import load_contracts as _load_w0_contracts

ANALYTICS_SCHEMA = "analytical-baseline.v1"
REQUIRED_COST_COMPONENTS = ("exchange", "clearing", "broker", "slippage", "funding", "exit")


def _scenario_dict(name: str, exch: float, fund: float) -> dict[str, Any]:
    return {
        "schema_version": "cost-profile.v1",
        "scenario": name,
        "status": "illustrative_unverified",
        "evidence_status": "account_tariff_and_historical_execution_unverified",
        "currency": "RUB",
        "components": {
            "exchange": {"value": exch, "unit": "rub_per_leg_side"},
            "clearing": {"value": exch, "unit": "rub_per_leg_side"},
            "broker": {"value": exch, "unit": "rub_per_leg_side"},
            "slippage": {"value": exch, "unit": "ticks_per_leg_side"},
            "funding": {"value": fund, "unit": "rub_per_leg_day"},
            "exit": {"value": exch, "unit": "rub_per_leg_side"},
        },
    }


COST_SCENARIOS: dict[str, dict[str, Any]] = {
    "low": _scenario_dict("low", 0.5, 0.25),
    "base": _scenario_dict("base", 1.0, 0.5),
    "stress": _scenario_dict("stress", 2.0, 1.0),
}


class AnalyticsError(W0Error):
    """Raised when analytical inputs cannot pass the baseline contract."""


def get_cost_scenario(name: str) -> dict[str, Any]:
    """Return a copy of an explicitly illustrative, unverified cost scenario."""
    try:
        return deepcopy(COST_SCENARIOS[name])
    except KeyError as exc:
        raise AnalyticsError(f"unknown cost scenario: {name}") from exc


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise AnalyticsError(f"{field} must be a finite number")
    number = float(value)
    if positive and number <= 0:
        raise AnalyticsError(f"{field} must be positive")
    return number


def _round(value: float) -> float:
    return round(float(value), 8)


def _validate_cost_profile(profile: dict[str, Any]) -> dict[str, Any]:
    if profile.get("schema_version") == COST_SCHEMA:
        return deepcopy(profile)
    if profile.get("schema_version") != "cost-profile.v1":
        raise AnalyticsError("unsupported cost profile schema")
    components = profile.get("components")
    if not isinstance(components, dict):
        raise AnalyticsError("cost profile components are required")
    normalized = deepcopy(profile)
    for component in REQUIRED_COST_COMPONENTS:
        item = normalized["components"].get(component)
        if not isinstance(item, dict):
            raise AnalyticsError(f"cost profile is missing {component}")
        item = cast(dict[str, Any], item)
        value = _number(item.get("value"), f"cost profile {component}")
        if value < 0:
            raise AnalyticsError(f"cost profile {component} cannot be negative")
        if not isinstance(item.get("unit"), str) or not item["unit"]:
            raise AnalyticsError(f"cost profile {component} unit is required")
        item["value"] = value
    return normalized


def _calendar_days(start_timestamp: str, end_timestamp: str) -> int:
    try:
        start = date.fromisoformat(start_timestamp[:10])
        end = date.fromisoformat(end_timestamp[:10])
    except (TypeError, ValueError) as exc:
        raise AnalyticsError("trade timestamps must start with ISO dates") from exc
    return max(1, (end - start).days)


def _trade_cost_breakdown(
    profile: dict[str, Any],
    *,
    entry_timestamp: str,
    exit_timestamp: str,
    quantity: int,
    near_contract: dict[str, Any],
    far_contract: dict[str, Any],
) -> dict[str, float]:
    components = profile["components"]
    legs = 2 * quantity
    slippage_ticks = float(components["slippage"]["value"])
    tick_sum = float(near_contract["tick_value"]) + float(far_contract["tick_value"])
    days = _calendar_days(entry_timestamp, exit_timestamp)
    return {
        "exchange": _round(float(components["exchange"]["value"]) * legs * 2),
        "clearing": _round(float(components["clearing"]["value"]) * legs * 2),
        "broker": _round(float(components["broker"]["value"]) * legs * 2),
        "slippage": _round(slippage_ticks * tick_sum * quantity * 2),
        "funding": _round(float(components["funding"]["value"]) * legs * days),
        "exit": _round(float(components["exit"]["value"]) * legs),
    }


@dataclass
class SimContext:
    params: Any
    near_contract: dict[str, Any]
    far_contract: dict[str, Any]
    near_multiplier: float
    far_multiplier: float
    spread_tick_value_rub: float
    margin_per_pair: float
    normalized_cost: dict[str, Any] | None
    tariff_costs: bool
    cost_components: tuple[str, ...]
    cost_status: str
    fast_intents: bool


def build_sim_context(series: dict[str, Any], params: Any) -> SimContext:
    rows = series.get("rows", [])
    if len(rows) < 2:
        raise AnalyticsError("fewer than two pair timestamps with benchmark marks")
    near_contract = series["contracts"][series["near"]]
    far_contract = series["contracts"][series["far"]]
    margin_per_pair = float(near_contract["initial_margin"]) + float(far_contract["initial_margin"])
    if not params.sizing_intents and margin_per_pair * params.quantity > params.initial_capital:
        raise AnalyticsError(
            f"ГО для выбранного размера ({margin_per_pair * params.quantity:.2f} ₽) превышает "
            f"стартовый капитал ({params.initial_capital:.2f} ₽). Допуск по стресс-тесту не пополняет счёт."
        )
    if params.fill == "open" and not all("near_open" in r and "far_open" in r for r in rows):
        raise AnalyticsError("open fills require open prices in the series")
    fast_intents = params.metrics_only and params.sizing_intents
    norm_cost = (
        None
        if fast_intents or params.cost_profile is None
        else _validate_cost_profile(params.cost_profile)
    )
    tariff_costs = norm_cost is not None and norm_cost["schema_version"] == COST_SCHEMA
    cost_status = (
        "unknown"
        if norm_cost is None
        else str(norm_cost["status"])
        if tariff_costs
        else "illustrative_unverified"
    )
    return SimContext(
        params=params,
        near_contract=near_contract,
        far_contract=far_contract,
        near_multiplier=float(near_contract["tick_value"]) / float(near_contract["tick_size"]),
        far_multiplier=float(far_contract["tick_value"]) / float(far_contract["tick_size"]),
        spread_tick_value_rub=float(
            series.get("spread_tick_value_rub", near_contract["tick_value"])
        ),
        margin_per_pair=margin_per_pair,
        normalized_cost=norm_cost,
        tariff_costs=tariff_costs,
        cost_components=COST_COMPONENTS if tariff_costs else REQUIRED_COST_COMPONENTS,
        cost_status=cost_status,
        fast_intents=fast_intents,
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
    row = cast(dict[str, Any], row)
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
        row = cast(dict[str, Any], row)
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
    if (
        contracts[near]["tick_size"] != contracts[far]["tick_size"]
        or contracts[near]["tick_value"] != contracts[far]["tick_value"]
    ):
        raise AnalyticsError("baseline requires equal tick sizes and tick values")
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


round_value = _round
trade_cost_breakdown = _trade_cost_breakdown

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
    "build_sim_context",
    "get_cost_scenario",
    "load_contracts",
    "round_value",
    "trade_cost_breakdown",
]
