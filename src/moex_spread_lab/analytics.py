"""Offline analytical spread baseline over ISS candle-close proxies."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .w0 import W0Error


ANALYTICS_SCHEMA = "analytical-baseline.v1"
REQUIRED_COST_COMPONENTS = ("exchange", "clearing", "broker", "slippage", "funding", "exit")


class AnalyticsError(W0Error):
    """Raised when analytical inputs cannot pass the baseline contract."""


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalyticsError(f"cannot read JSON input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AnalyticsError(f"JSON input is not an object: {path}")
    return value, hashlib.sha256(raw).hexdigest()


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise AnalyticsError(f"{field} must be a finite number")
    number = float(value)
    if positive and number <= 0:
        raise AnalyticsError(f"{field} must be positive")
    return number


def _round(value: float) -> float:
    return round(float(value), 8)


def _date_key(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) < 10:
        raise AnalyticsError(f"{field} must contain an ISO date/timestamp")
    return value[:10]


def load_contracts(path: Path, near: str, far: str) -> tuple[dict[str, Any], str]:
    payload, digest = _read_json(path)
    instruments = {row.get("secid"): row for row in payload.get("instruments", []) if isinstance(row, dict)}
    selected = {}
    for secid in (near, far):
        instrument = instruments.get(secid)
        if instrument is None:
            raise AnalyticsError(f"contract is missing from intake: {secid}")
        for field in ("lot_size", "tick_size", "tick_value", "initial_margin", "source_vintage"):
            if field not in instrument:
                raise AnalyticsError(f"contract {secid} is missing {field}")
        selected[secid] = {
            "secid": secid,
            "stable_id": instrument.get("stable_id"),
            "board": instrument.get("board"),
            "expiry": instrument.get("expiry"),
            "lot_size": int(_number(instrument["lot_size"], f"{secid}.lot_size", positive=True)),
            "tick_size": _number(instrument["tick_size"], f"{secid}.tick_size", positive=True),
            "tick_value": _number(instrument["tick_value"], f"{secid}.tick_value", positive=True),
            "initial_margin": _number(instrument["initial_margin"], f"{secid}.initial_margin", positive=True),
            "source_vintage": instrument["source_vintage"],
            "historical_evidence": instrument.get("historical_evidence", False),
            "historical_specification_basis": instrument.get("historical_specification_basis"),
        }
    return selected, digest


def _load_candles(path: Path, secid: str) -> tuple[dict[str, dict[str, Any]], str]:
    payload, digest = _read_json(path)
    if payload.get("schema_version") != "iss-candles.v1" or payload.get("secid") != secid:
        raise AnalyticsError(f"unsupported candle input for {secid}: {path}")
    if payload.get("interval") != 24:
        raise AnalyticsError(f"baseline requires daily candles for {secid}")
    result = {}
    for row in payload.get("rows", []):
        if not isinstance(row, dict):
            raise AnalyticsError(f"malformed candle row for {secid}")
        date_key = _date_key(row.get("begin"), f"{secid}.begin")
        close = _number(row.get("close"), f"{secid}.{date_key}.close", positive=True)
        if row.get("price_proxy_kind") != "candle_close":
            raise AnalyticsError(f"candle row does not declare close proxy: {secid} {date_key}")
        result[date_key] = {
            "date": date_key,
            "close": close,
            "source": row.get("source"),
            "price_proxy_kind": row.get("price_proxy_kind"),
        }
    if not result:
        raise AnalyticsError(f"empty candle input for {secid}")
    return result, digest


def _load_benchmark(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    payload, digest = _read_json(path)
    if payload.get("schema_version") != "lqdt-lineage.v1":
        raise AnalyticsError("unsupported LQDT benchmark schema")
    result = {}
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


def build_daily_series(
    near_path: Path,
    far_path: Path,
    benchmark_path: Path,
    contracts_path: Path,
    *,
    near: str = "CNYRUBF",
    far: str = "CRZ6",
) -> dict[str, Any]:
    near_rows, near_hash = _load_candles(near_path, near)
    far_rows, far_hash = _load_candles(far_path, far)
    benchmark, benchmark_hash = _load_benchmark(benchmark_path)
    contracts, contracts_hash = load_contracts(contracts_path, near, far)
    common_dates = sorted(set(near_rows) & set(far_rows) & set(benchmark))
    if len(common_dates) < 2:
        raise AnalyticsError("fewer than two common pair and benchmark dates")
    near_contract = contracts[near]
    far_contract = contracts[far]
    if near_contract["tick_size"] != far_contract["tick_size"]:
        raise AnalyticsError("baseline requires equal tick sizes")
    rows = []
    for date_key in common_dates:
        near_close = near_rows[date_key]["close"]
        far_close = far_rows[date_key]["close"]
        spread_value = near_close - far_close
        rows.append({
            "date": date_key,
            "near_close": near_close,
            "far_close": far_close,
            "spread_value": _round(spread_value),
            "spread_ticks": _round(spread_value / near_contract["tick_size"]),
            "benchmark_price": benchmark[date_key]["price"],
            "benchmark_source_ticker": benchmark[date_key]["source_ticker"],
            "benchmark_source_kind": benchmark[date_key]["source_kind"],
            "near_price_proxy_kind": near_rows[date_key]["price_proxy_kind"],
            "far_price_proxy_kind": far_rows[date_key]["price_proxy_kind"],
        })
    return {
        "schema_version": "analytical-series.v1",
        "near": near,
        "far": far,
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


def _rolling_stats(values: list[float]) -> tuple[float, float]:
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return mean, math.sqrt(variance)


def run_mean_reversion_baseline(
    series: dict[str, Any],
    *,
    initial_capital: float = 30000.0,
    lookback: int = 20,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
    quantity: int = 1,
    cost_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if initial_capital <= 0 or lookback < 2 or entry_z <= 0 or exit_z < 0 or quantity <= 0:
        raise AnalyticsError("invalid baseline parameters")
    rows = series.get("rows", [])
    contracts = series["contracts"]
    near = series["near"]
    far = series["far"]
    near_contract = contracts[near]
    far_contract = contracts[far]
    margin_per_pair = near_contract["initial_margin"] + far_contract["initial_margin"]
    if margin_per_pair * quantity > initial_capital:
        raise AnalyticsError("minimum baseline quantity exceeds initial capital margin envelope")
    near_multiplier = near_contract["tick_value"] / near_contract["tick_size"]
    far_multiplier = far_contract["tick_value"] / far_contract["tick_size"]
    cost_status = "unknown"
    costs_per_round_trip: float | None = None
    if cost_profile is not None:
        if all(isinstance(cost_profile.get(component), (int, float)) for component in REQUIRED_COST_COMPONENTS):
            costs_per_round_trip = sum(float(cost_profile[component]) for component in REQUIRED_COST_COMPONENTS) * 2 * quantity
            cost_status = "illustrative_numeric_profile"
        else:
            cost_status = "unknown_components"

    benchmark_start = rows[0]["benchmark_price"]
    position = 0
    entry_near = entry_far = 0.0
    entry_date: str | None = None
    realized_gross = 0.0
    realized_costs = 0.0
    trades: list[dict[str, Any]] = []
    daily = []
    displayed_cost = lambda value: _round(value) if costs_per_round_trip is not None else None

    def pnl_at(current_near: float, current_far: float) -> float:
        if position == 0:
            return 0.0
        return quantity * position * ((current_near - entry_near) * near_multiplier - (current_far - entry_far) * far_multiplier)

    def close_trade(date_key: str, current_near: float, current_far: float, reason: str) -> None:
        nonlocal position, entry_near, entry_far, entry_date, realized_gross, realized_costs
        gross = pnl_at(current_near, current_far)
        cost = costs_per_round_trip if costs_per_round_trip is not None else 0.0
        realized_gross += gross
        realized_costs += cost
        trades.append({
            "entry_date": entry_date,
            "exit_date": date_key,
            "direction": "long_near_short_far" if position > 0 else "short_near_long_far",
            "quantity": quantity,
            "entry_spread_ticks": _round((entry_near - entry_far) / near_contract["tick_size"]),
            "exit_spread_ticks": _round((current_near - current_far) / near_contract["tick_size"]),
            "gross_pnl": _round(gross),
            "modeled_costs": displayed_cost(cost),
            "net_pnl": _round(gross - cost) if costs_per_round_trip is not None else None,
            "exit_reason": reason,
        })
        position = 0
        entry_date = None

    for index, row in enumerate(rows):
        action = "hold_flat" if position == 0 else "hold_position"
        signal = None
        signal_z = None
        if index > lookback:
            signal_index = index - 1
            window = [float(item["spread_ticks"]) for item in rows[signal_index - lookback:signal_index]]
            mean, standard_deviation = _rolling_stats(window)
            deviation = float(rows[signal_index]["spread_ticks"]) - mean
            signal_z = deviation / standard_deviation if standard_deviation > 0 else 0.0
            if position != 0 and abs(signal_z) <= exit_z:
                close_trade(row["date"], row["near_close"], row["far_close"], "mean_reversion_exit")
                action = "exit"
            elif position == 0 and index < len(rows) - 1 and abs(signal_z) >= entry_z:
                position = 1 if signal_z < 0 else -1
                entry_near = row["near_close"]
                entry_far = row["far_close"]
                entry_date = row["date"]
                action = "enter_long_spread" if position > 0 else "enter_short_spread"
            else:
                signal = "entry_candidate" if position == 0 and abs(signal_z) >= entry_z else "no_action"

        benchmark_value = initial_capital * row["benchmark_price"] / benchmark_start
        strategy_value = initial_capital + realized_gross - realized_costs + pnl_at(row["near_close"], row["far_close"])
        daily.append({
            "date": row["date"],
            "spread_ticks": row["spread_ticks"],
            "signal_z": _round(signal_z) if signal_z is not None else None,
            "action": action,
            "position": position,
            "strategy_value": _round(strategy_value),
            "benchmark_value": _round(benchmark_value),
            "X": _round(strategy_value - benchmark_value),
            "gross_realized_pnl": _round(realized_gross),
            "modeled_costs": displayed_cost(realized_costs),
            "benchmark_source_kind": row["benchmark_source_kind"],
        })

    if position != 0:
        last = rows[-1]
        close_trade(last["date"], last["near_close"], last["far_close"], "end_of_sample")
        daily[-1]["action"] = "forced_exit_end_of_sample"
        daily[-1]["position"] = 0
        daily[-1]["strategy_value"] = _round(initial_capital + realized_gross - realized_costs)
        daily[-1]["X"] = _round(daily[-1]["strategy_value"] - daily[-1]["benchmark_value"])
        daily[-1]["gross_realized_pnl"] = _round(realized_gross)
        daily[-1]["modeled_costs"] = displayed_cost(realized_costs)

    peak = 0.0
    for row in daily:
        peak = max(peak, row["X"])
        row["H"] = _round(peak)
        row["D"] = _round(peak - row["X"])
    final = daily[-1]
    net_status = "usable_for_gross_only" if cost_status != "illustrative_numeric_profile" else "illustrative_net_sensitivity"
    return {
        "schema_version": ANALYTICS_SCHEMA,
        "release_state": "exploratory_inconclusive",
        "near": near,
        "far": far,
        "direction": "near_minus_far",
        "timeframe": "1d",
        "price_interpretation": "ISS candle close analytical proxy under A-06",
        "execution_evidence_status": "historical_bid_ask_unavailable",
        "parameters": {
            "initial_capital": initial_capital,
            "lookback": lookback,
            "entry_z": entry_z,
            "exit_z": exit_z,
            "quantity": quantity,
            "decision_delay_bars": 1,
            "margin_per_pair": _round(margin_per_pair),
            "margin_gate": "passed",
        },
        "economics": {
            "price_unit": "RUB_per_CNY",
            "near_lot_size": near_contract["lot_size"],
            "far_lot_size": far_contract["lot_size"],
            "near_tick_size": near_contract["tick_size"],
            "far_tick_size": far_contract["tick_size"],
            "near_tick_value_rub": near_contract["tick_value"],
            "far_tick_value_rub": far_contract["tick_value"],
            "pair_initial_margin_rub": _round(margin_per_pair * quantity),
            "one_spread_tick_value_rub": _round(near_contract["tick_value"]),
            "contract_terms_status": "current_snapshot_fallback_assumed",
        },
        "costs": {
            "status": cost_status,
            "components": list(REQUIRED_COST_COMPONENTS),
            "profile": cost_profile,
            "net_status": net_status,
        },
        "summary": {
            "common_row_count": len(rows),
            "first_date": rows[0]["date"],
            "last_date": rows[-1]["date"],
            "trade_count": len(trades),
            "gross_pnl": _round(realized_gross),
            "modeled_costs": displayed_cost(realized_costs),
            "costs_applied": costs_per_round_trip is not None,
            "final_strategy_value": final["strategy_value"],
            "final_benchmark_value": final["benchmark_value"],
            "final_X": final["X"],
            "max_drawdown_D": _round(max(row["D"] for row in daily)),
            "min_X": _round(min(row["X"] for row in daily)),
            "benchmark_source_kinds": sorted({row["benchmark_source_kind"] for row in daily}),
        },
        "trades": trades,
        "daily": daily,
        "input_provenance": series["provenance"],
    }


def write_analytics(result: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
