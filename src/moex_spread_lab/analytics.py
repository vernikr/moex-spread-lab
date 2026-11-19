"""Offline analytical spread baseline over ISS candle-close proxies."""

from __future__ import annotations

import hashlib
import math
from bisect import bisect_left
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from .costs import COST_COMPONENTS, COST_SCHEMA, side_cost
from .portfolio import lqdt_overlay_values
from .w0 import W0Error, read_hashed_json

ANALYTICS_SCHEMA = "analytical-baseline.v1"
REQUIRED_COST_COMPONENTS = ("exchange", "clearing", "broker", "slippage", "funding", "exit")
COST_SCENARIOS = {
    "low": {
        "schema_version": "cost-profile.v1",
        "scenario": "low",
        "status": "illustrative_unverified",
        "evidence_status": "account_tariff_and_historical_execution_unverified",
        "currency": "RUB",
        "components": {
            "exchange": {"value": 0.5, "unit": "rub_per_leg_side"},
            "clearing": {"value": 0.5, "unit": "rub_per_leg_side"},
            "broker": {"value": 0.5, "unit": "rub_per_leg_side"},
            "slippage": {"value": 0.5, "unit": "ticks_per_leg_side"},
            "funding": {"value": 0.25, "unit": "rub_per_leg_day"},
            "exit": {"value": 0.5, "unit": "rub_per_leg_side"},
        },
    },
    "base": {
        "schema_version": "cost-profile.v1",
        "scenario": "base",
        "status": "illustrative_unverified",
        "evidence_status": "account_tariff_and_historical_execution_unverified",
        "currency": "RUB",
        "components": {
            "exchange": {"value": 1.0, "unit": "rub_per_leg_side"},
            "clearing": {"value": 1.0, "unit": "rub_per_leg_side"},
            "broker": {"value": 1.0, "unit": "rub_per_leg_side"},
            "slippage": {"value": 1.0, "unit": "ticks_per_leg_side"},
            "funding": {"value": 0.5, "unit": "rub_per_leg_day"},
            "exit": {"value": 1.0, "unit": "rub_per_leg_side"},
        },
    },
    "stress": {
        "schema_version": "cost-profile.v1",
        "scenario": "stress",
        "status": "illustrative_unverified",
        "evidence_status": "account_tariff_and_historical_execution_unverified",
        "currency": "RUB",
        "components": {
            "exchange": {"value": 2.0, "unit": "rub_per_leg_side"},
            "clearing": {"value": 2.0, "unit": "rub_per_leg_side"},
            "broker": {"value": 2.0, "unit": "rub_per_leg_side"},
            "slippage": {"value": 2.0, "unit": "ticks_per_leg_side"},
            "funding": {"value": 1.0, "unit": "rub_per_leg_day"},
            "exit": {"value": 2.0, "unit": "rub_per_leg_side"},
        },
    },
}


class AnalyticsError(W0Error):
    """Raised when analytical inputs cannot pass the baseline contract."""


def get_cost_scenario(name: str) -> dict[str, Any]:
    """Return a copy of an explicitly illustrative, unverified cost scenario."""
    try:
        return deepcopy(COST_SCENARIOS[name])
    except KeyError as exc:
        raise AnalyticsError(f"unknown cost scenario: {name}") from exc


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
        item = components.get(component)
        if not isinstance(item, dict):
            raise AnalyticsError(f"cost profile is missing {component}")
        value = _number(item.get("value"), f"cost profile {component}")
        if value < 0:
            raise AnalyticsError(f"cost profile {component} cannot be negative")
        if not isinstance(item.get("unit"), str) or not item["unit"]:
            raise AnalyticsError(f"cost profile {component} unit is required")
        item["value"] = value
    return normalized


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise AnalyticsError(f"{field} must be a finite number")
    number = float(value)
    if positive and number <= 0:
        raise AnalyticsError(f"{field} must be positive")
    return number


def _round(value: float) -> float:
    return round(float(value), 8)


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
    exchange = float(components["exchange"]["value"]) * legs * 2
    clearing = float(components["clearing"]["value"]) * legs * 2
    broker = float(components["broker"]["value"]) * legs * 2
    slippage_ticks = float(components["slippage"]["value"])
    slippage = slippage_ticks * (near_contract["tick_value"] + far_contract["tick_value"]) * quantity * 2
    funding = float(components["funding"]["value"]) * legs * _calendar_days(entry_timestamp, exit_timestamp)
    exit_cost = float(components["exit"]["value"]) * legs
    return {
        "exchange": _round(exchange),
        "clearing": _round(clearing),
        "broker": _round(broker),
        "slippage": _round(slippage),
        "funding": _round(funding),
        "exit": _round(exit_cost),
    }


def _date_key(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) < 10:
        raise AnalyticsError(f"{field} must contain an ISO date/timestamp")
    return value[:10]


def load_contracts(path: Path, near: str, far: str) -> tuple[dict[str, Any], str]:
    payload, digest = read_hashed_json(path, error=AnalyticsError)
    instruments = {row.get("secid"): row for row in payload.get("instruments", []) if isinstance(row, dict)}
    historical_path = Path(path).parent / "historical-contracts.json"
    if historical_path.is_file():
        historical, historical_digest = read_hashed_json(historical_path, error=AnalyticsError)
        instruments.update({row.get("secid"): row for row in historical.get("instruments", []) if isinstance(row, dict)})
        digest = hashlib.sha256(f"{digest}:{historical_digest}".encode("ascii")).hexdigest()
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
            "tariff_group": instrument.get("tariff_group"),
            "historical_evidence": instrument.get("historical_evidence", False),
            "historical_specification_basis": instrument.get("historical_specification_basis"),
        }
    return selected, digest


def _load_candles(path: Path, secid: str, interval: int) -> tuple[dict[str, dict[str, Any]], str]:
    payload, digest = read_hashed_json(path, error=AnalyticsError)
    if payload.get("schema_version") != "iss-candles.v1" or payload.get("secid") != secid:
        raise AnalyticsError(f"unsupported candle input for {secid}: {path}")
    if payload.get("interval") != interval:
        raise AnalyticsError(f"candle interval mismatch for {secid}: expected {interval}")
    result = {}
    for row in payload.get("rows", []):
        if not isinstance(row, dict):
            raise AnalyticsError(f"malformed candle row for {secid}")
        timestamp = row.get("begin")
        date_key = _date_key(timestamp, f"{secid}.begin")
        if not isinstance(timestamp, str) or len(timestamp) < 19:
            raise AnalyticsError(f"{secid}.begin must contain a timestamp")
        timestamp = timestamp[:19]
        close = _number(row.get("close"), f"{secid}.{timestamp}.close", positive=True)
        if row.get("price_proxy_kind") != "candle_close":
            raise AnalyticsError(f"candle row does not declare close proxy: {secid} {date_key}")
        result[timestamp] = {
            "date": date_key,
            "timestamp": timestamp,
            "open": float(row["open"]) if isinstance(row.get("open"), (int, float)) and row["open"] > 0 else close,
            "close": close,
            "source": row.get("source"),
            "price_proxy_kind": row.get("price_proxy_kind"),
        }
    if not result:
        raise AnalyticsError(f"empty candle input for {secid}")
    return result, digest


def _load_benchmark(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    payload, digest = read_hashed_json(path, error=AnalyticsError)
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
    common_timestamps = sorted(set(near_rows) & set(far_rows))
    if len(common_timestamps) < 2:
        raise AnalyticsError("fewer than two common pair timestamps")
    last_timestamp_by_date: dict[str, str] = {}
    for timestamp in common_timestamps:
        last_timestamp_by_date[near_rows[timestamp]["date"]] = timestamp
    benchmark_dates = sorted(benchmark)

    def benchmark_mark(timestamp: str, date_key: str) -> tuple[dict[str, Any] | None, str]:
        if interval == 24 or last_timestamp_by_date[date_key] == timestamp:
            return benchmark.get(date_key), "daily_close_same_date"
        prior_index = bisect_left(benchmark_dates, date_key) - 1
        return benchmark.get(benchmark_dates[prior_index]) if prior_index >= 0 else None, "prior_daily_close"

    near_contract = contracts[near]
    far_contract = contracts[far]
    if near_contract["tick_size"] != far_contract["tick_size"]:
        raise AnalyticsError("baseline requires equal tick sizes")
    rows = []
    for timestamp in common_timestamps:
        date_key = near_rows[timestamp]["date"]
        benchmark_row, benchmark_mark_kind = benchmark_mark(timestamp, date_key)
        if benchmark_row is None:
            continue
        near_close = near_rows[timestamp]["close"]
        far_close = far_rows[timestamp]["close"]
        spread_value = near_close - far_close
        rows.append({
            "date": date_key,
            "timestamp": timestamp,
            "near_close": near_close,
            "far_close": far_close,
            "near_open": near_rows[timestamp]["open"],
            "far_open": far_rows[timestamp]["open"],
            "spread_value": _round(spread_value),
            "spread_ticks": _round(spread_value / near_contract["tick_size"]),
            "benchmark_price": benchmark_row["price"],
            "benchmark_source_ticker": benchmark_row["source_ticker"],
            "benchmark_source_kind": benchmark_row["source_kind"],
            "benchmark_mark_kind": benchmark_mark_kind,
            "near_price_proxy_kind": near_rows[timestamp]["price_proxy_kind"],
            "far_price_proxy_kind": far_rows[timestamp]["price_proxy_kind"],
        })
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
    return build_series(near_path, far_path, benchmark_path, contracts_path, near=near, far=far, interval=24)


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
    decision_delay_bars: int = 1,
    analysis_role: str = "primary_baseline",
    cost_profile: dict[str, Any] | None = None,
    fill: str = "close",
    cash_in_lqdt: bool = False,
    funding_swaprates: dict[str, float] | None = None,
    sizing_intents: bool = False,
) -> dict[str, Any]:
    """Run the delayed z-score control.

    `fill="open"` executes at the open of the bar after the signal bar; `cash_in_lqdt` values the
    account as free cash in LQDT plus margin cash (A-11) instead of idle cash. When supplied,
    `funding_swaprates` maps dates to the near perpetual's MOEX EOD SWAPRATE per underlying unit;
    signed RUB funding is charged to the modeled near-leg position at the day's proxy clearing.
    Internal diagnostic `sizing_intents` emits unit signal intents only; the proof adapter
    must replay them through its funded account before publishing a portfolio result.
    """
    if (
        initial_capital <= 0
        or lookback < 2
        or entry_z <= 0
        or exit_z < 0
        or quantity <= 0
        or decision_delay_bars < 0
        or (decision_delay_bars == 0 and (analysis_role != "diagnostic_only" or fill != "close"))
        or analysis_role not in {"primary_baseline", "diagnostic_only"}
        or fill not in {"close", "open"}
        or (sizing_intents and (analysis_role != "diagnostic_only" or quantity != 1))
    ):
        raise AnalyticsError("invalid baseline parameters")
    rows = series.get("rows", [])
    contracts = series["contracts"]
    near = series["near"]
    far = series["far"]
    near_contract = contracts[near]
    far_contract = contracts[far]
    margin_per_pair = near_contract["initial_margin"] + far_contract["initial_margin"]
    if not sizing_intents and margin_per_pair * quantity > initial_capital:
        raise AnalyticsError(
            f"ГО для выбранного размера ({margin_per_pair * quantity:.2f} ₽) превышает "
            f"стартовый капитал ({initial_capital:.2f} ₽). Допуск по стресс-тесту не пополняет счёт."
        )
    near_multiplier = near_contract["tick_value"] / near_contract["tick_size"]
    far_multiplier = far_contract["tick_value"] / far_contract["tick_size"]
    normalized_cost_profile = _validate_cost_profile(cost_profile) if cost_profile is not None else None
    tariff_costs = normalized_cost_profile is not None and normalized_cost_profile["schema_version"] == COST_SCHEMA
    cost_components = COST_COMPONENTS if tariff_costs else REQUIRED_COST_COMPONENTS
    cost_status = (
        "unknown" if normalized_cost_profile is None
        else normalized_cost_profile["status"] if tariff_costs else "illustrative_unverified"
    )
    if fill == "open" and not all("near_open" in row and "far_open" in row for row in rows):
        raise AnalyticsError("open fills require open prices in the series")

    benchmark_start = rows[0]["benchmark_price"]
    position = 0
    entry_near = entry_far = 0.0
    entry_date: str | None = None
    realized_gross = 0.0
    realized_costs = 0.0
    cumulative_funding = 0.0
    trade_funding = 0.0
    funding_swaprates = funding_swaprates or {}
    trades: list[dict[str, Any]] = []
    daily = []
    cost_totals = {component: 0.0 for component in cost_components}
    entry_breakdown: dict[str, float] | None = None
    entry_signal_z: float | None = None

    def pnl_at(current_near: float, current_far: float) -> float:
        if position == 0:
            return 0.0
        return quantity * position * ((current_near - entry_near) * near_multiplier - (current_far - entry_far) * far_multiplier)

    def close_trade(date_key: str, current_near: float, current_far: float, reason: str) -> None:
        nonlocal position, entry_near, entry_far, entry_date, realized_gross, realized_costs, entry_breakdown, entry_signal_z, trade_funding
        price_gross = pnl_at(current_near, current_far)
        gross = price_gross + trade_funding
        exit_breakdown = (
            side_cost(normalized_cost_profile, [(near_contract, current_near), (far_contract, current_far)], quantity)
            if tariff_costs
            else None
        )
        breakdown = (
            {key: entry_breakdown[key] + exit_breakdown[key] for key in exit_breakdown}
            if tariff_costs
            else _trade_cost_breakdown(
                normalized_cost_profile,
                entry_timestamp=entry_date or date_key,
                exit_timestamp=date_key,
                quantity=quantity,
                near_contract=near_contract,
                far_contract=far_contract,
            )
            if normalized_cost_profile is not None
            else None
        )
        cost = sum(breakdown.values()) if breakdown is not None else 0.0
        realized_gross += price_gross
        realized_costs += sum(exit_breakdown.values()) if tariff_costs else cost
        if breakdown is not None:
            for component, value in (exit_breakdown if tariff_costs else breakdown).items():
                cost_totals[component] += value
        trades.append({
            "entry_date": entry_date,
            "exit_date": date_key,
            "direction": "long_near_short_far" if position > 0 else "short_near_long_far",
            "quantity": quantity,
            "entry_spread_ticks": _round((entry_near - entry_far) / near_contract["tick_size"]),
            "exit_spread_ticks": _round((current_near - current_far) / near_contract["tick_size"]),
            "entry_near_fill": entry_near,
            "entry_far_fill": entry_far,
            "exit_near_fill": current_near,
            "exit_far_fill": current_far,
            "entry_spread_fill": _round(entry_near * near_multiplier - entry_far * far_multiplier),
            "exit_spread_fill": _round(current_near * near_multiplier - current_far * far_multiplier),
            "entry_signal_z": _round(entry_signal_z) if entry_signal_z is not None else None,
            "gross_pnl": _round(gross),
            "price_gross_pnl": _round(price_gross),
            "funding_rub": _round(trade_funding),
            "modeled_costs": _round(cost) if breakdown is not None else None,
            "cost_breakdown": breakdown,
            "entry_costs": {key: _round(value) for key, value in entry_breakdown.items()} if entry_breakdown is not None else None,
            "exit_costs": {key: _round(value) for key, value in exit_breakdown.items()} if exit_breakdown is not None else None,
            "net_pnl": _round(gross - cost) if breakdown is not None else None,
            "exit_reason": reason,
        })
        position = 0
        entry_date = None
        trade_funding = 0.0
        entry_breakdown = None
        entry_signal_z = None

    for index, row in enumerate(rows):
        action = "hold_flat" if position == 0 else "hold_position"
        signal_z = None
        signal_index = index - decision_delay_bars
        if signal_index >= lookback:
            window = [float(item["spread_ticks"]) for item in rows[signal_index - lookback:signal_index]]
            mean, standard_deviation = _rolling_stats(window)
            deviation = float(rows[signal_index]["spread_ticks"]) - mean
            signal_z = deviation / standard_deviation if standard_deviation > 0 else 0.0
            fill_near, fill_far = (row["near_open"], row["far_open"]) if fill == "open" else (row["near_close"], row["far_close"])
            if position != 0 and abs(signal_z) <= exit_z:
                close_trade(row.get("timestamp", row["date"]), fill_near, fill_far, "mean_reversion_exit")
                action = "exit"
            elif position == 0 and index < len(rows) - 1 and abs(signal_z) >= entry_z:
                position = 1 if signal_z < 0 else -1
                entry_near, entry_far = fill_near, fill_far
                entry_date = row.get("timestamp", row["date"])
                entry_signal_z = signal_z
                if tariff_costs:
                    entry_breakdown = side_cost(normalized_cost_profile, [(near_contract, fill_near), (far_contract, fill_far)], quantity)
                    realized_costs += sum(entry_breakdown.values())
                    for component, value in entry_breakdown.items():
                        cost_totals[component] += value
                action = "enter_long_spread" if position > 0 else "enter_short_spread"

        funding_today = 0.0
        swaprate = funding_swaprates.get(row["date"])
        if position and swaprate is not None:
            funding_today = -position * float(swaprate) * float(near_contract["lot_size"]) * quantity
            trade_funding += funding_today
            cumulative_funding += funding_today
        benchmark_value = initial_capital * row["benchmark_price"] / benchmark_start
        strategy_value = initial_capital + realized_gross - realized_costs + cumulative_funding + pnl_at(row["near_close"], row["far_close"])
        daily.append({
            "date": row["date"],
            "timestamp": row.get("timestamp", row["date"]),
            "spread_ticks": row["spread_ticks"],
            "signal_z": _round(signal_z) if signal_z is not None else None,
            "action": action,
            "position": position,
            "strategy_value": _round(strategy_value),
            "benchmark_value": _round(benchmark_value),
            "X": _round(strategy_value - benchmark_value),
            "gross_realized_pnl": _round(realized_gross + cumulative_funding),
            "funding_rub": _round(funding_today),
            "cumulative_funding_rub": _round(cumulative_funding),
            "modeled_costs": _round(realized_costs) if normalized_cost_profile is not None else None,
            "benchmark_source_kind": row["benchmark_source_kind"],
            "benchmark_mark_kind": row.get("benchmark_mark_kind", "daily_close_same_date"),
        })

    if position != 0:
        last = rows[-1]
        close_trade(last.get("timestamp", last["date"]), last["near_close"], last["far_close"], "end_of_sample")
        daily[-1]["action"] = "forced_exit_end_of_sample"
        daily[-1]["position"] = 0
        daily[-1]["strategy_value"] = _round(initial_capital + realized_gross + cumulative_funding - realized_costs)
        daily[-1]["X"] = _round(daily[-1]["strategy_value"] - daily[-1]["benchmark_value"])
        daily[-1]["gross_realized_pnl"] = _round(realized_gross + cumulative_funding)
        daily[-1]["modeled_costs"] = _round(realized_costs) if normalized_cost_profile is not None else None

    cash_shortfall_rows = 0
    if cash_in_lqdt:
        values, cash_shortfall_rows = lqdt_overlay_values(
            initial_capital,
            [row["strategy_value"] - initial_capital for row in daily],
            [margin_per_pair * quantity if row["position"] else 0.0 for row in daily],
            [row["benchmark_price"] for row in rows],
        )
        for row, value in zip(daily, values):
            row["strategy_value"] = _round(value)
            row["X"] = _round(value - row["benchmark_value"])

    peak = 0.0
    for row in daily:
        peak = max(peak, row["X"])
        row["H"] = _round(peak)
        row["D"] = _round(peak - row["X"])
    final = daily[-1]
    net_status = (
        "usable_for_gross_only" if normalized_cost_profile is None
        else "published_tariff_net" if tariff_costs else "illustrative_net_sensitivity"
    )
    return {
        "schema_version": ANALYTICS_SCHEMA,
        "release_state": "unit_signal_intents_not_portfolio" if sizing_intents else "exploratory_inconclusive",
        "near": near,
        "far": far,
        "direction": series.get("direction", "near_minus_far"),
        "analysis_role": analysis_role,
        "timeframe": series.get("timeframe", "1d"),
        "price_interpretation": "ISS candle close analytical proxy under A-06",
        "execution_evidence_status": "historical_bid_ask_unavailable",
        "parameters": {
            "initial_capital": initial_capital,
            "lookback": lookback,
            "entry_z": entry_z,
            "exit_z": exit_z,
            "quantity": quantity,
            "decision_delay_bars": decision_delay_bars,
            "fill": fill,
            "accounting": "free_cash_in_lqdt_margin_cash_idle" if cash_in_lqdt else "idle_cash_no_yield",
            "margin_per_pair": _round(margin_per_pair),
            "margin_gate": "deferred_to_funded_sizing" if sizing_intents else "passed",
        },
        "economics": {
            "price_unit": series.get("price_unit", "RUB_per_CNY"),
            "near_lot_size": near_contract["lot_size"],
            "far_lot_size": far_contract["lot_size"],
            "near_tick_size": near_contract["tick_size"],
            "far_tick_size": far_contract["tick_size"],
            "near_tick_value_rub": near_contract["tick_value"],
            "far_tick_value_rub": far_contract["tick_value"],
            "pair_initial_margin_rub": _round(margin_per_pair * quantity),
            "one_spread_tick_value_rub": _round(series.get("spread_tick_value_rub", near_contract["tick_value"])),
            "contract_terms_status": "current_snapshot_fallback_assumed",
        },
        "costs": {
            "status": cost_status,
            "components": list(cost_components),
            "profile": normalized_cost_profile,
            "total_by_component": {
                component: _round(value) for component, value in cost_totals.items()
            } if normalized_cost_profile is not None else None,
            "net_status": net_status,
        },
        "summary": {
            "common_row_count": len(rows),
            "first_date": rows[0]["date"],
            "last_date": rows[-1]["date"],
            "trade_count": len(trades),
            "gross_pnl": _round(realized_gross + cumulative_funding),
            "price_gross_pnl": _round(realized_gross),
            "funding_rub": _round(cumulative_funding),
            "modeled_costs": _round(realized_costs) if normalized_cost_profile is not None else None,
            "costs_applied": normalized_cost_profile is not None,
            "final_strategy_value": final["strategy_value"],
            "final_benchmark_value": final["benchmark_value"],
            "final_X": final["X"],
            "max_drawdown_D": _round(max(row["D"] for row in daily)),
            "min_X": _round(min(row["X"] for row in daily)),
            "benchmark_source_kinds": sorted({row["benchmark_source_kind"] for row in daily}),
            "benchmark_mark_kinds": sorted({row.get("benchmark_mark_kind", "daily_close_same_date") for row in daily}),
            "cash_shortfall_rows": cash_shortfall_rows,
        },
        "trades": trades,
        "daily": daily,
        "input_provenance": series["provenance"],
    }
