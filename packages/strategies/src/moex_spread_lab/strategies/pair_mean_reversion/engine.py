"""Simulation, reporting, and causal entry-band logic for pair mean reversion."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from moex_spread_lab.accounting.costs import lqdt_overlay_values, side_cost
from moex_spread_lab.core.contracts import EntryBandPoint

from ..base import population_rolling_stats
from .fast_scan import (
    BaselineParams,
    compute_rolling_z,
    parse_baseline_params,
    try_fast_intents_scan,
)
from .series_builder import (
    ANALYTICS_SCHEMA,
    COST_SCENARIOS,
    REQUIRED_COST_COMPONENTS,
    AnalyticsError,
    SimContext,
    _calendar_days,
    _date_key,
    _load_benchmark,
    _load_candles,
    _number,
    _round,
    _trade_cost_breakdown,
    _validate_cost_profile,
    build_daily_series,
    build_series,
    build_sim_context,
    get_cost_scenario,
    load_contracts,
)

_rolling_stats = population_rolling_stats


def compute_pair_entry_bands(
    series: list[dict[str, Any]],
    params: Mapping[str, Any],
    fill: str,
    *,
    far_point_value_rub: float = 1.0,
) -> list[EntryBandPoint]:
    """Compute causal population-std entry bands excluding the current signal bar."""
    window = int(params["lookback"])
    entry_z = float(params["entry_z"])
    history: list[float] = []
    points: list[EntryBandPoint] = []
    for row in series:
        date_str = str(row.get("date", ""))
        if not row.get("volume_eligible", True):
            points.append(EntryBandPoint(date=date_str))
            continue
        value = float(row["spread"])
        sample = history[-window:]
        buy_level = sell_level = buy_rel = sell_rel = None
        if len(sample) == window:
            mean, std = population_rolling_stats(sample)
            if std > 0:
                width = entry_z * std
                buy_level = mean - width
                sell_level = mean + width
                far_val = float(row["far"]) * far_point_value_rub
                if far_val:
                    buy_rel = buy_level / far_val * 100
                    sell_rel = sell_level / far_val * 100
        history.append(value)
        points.append(
            EntryBandPoint(
                date=date_str,
                buy_level=buy_level,
                sell_level=sell_level,
                buy_level_rel=buy_rel,
                sell_level_rel=sell_rel,
            )
        )
    return points


@dataclass
class _SimState:
    cost_totals: dict[str, float]
    position: int = 0
    entry_near: float = 0.0
    entry_far: float = 0.0
    entry_date: str | None = None
    realized_gross: float = 0.0
    realized_costs: float = 0.0
    cumulative_funding: float = 0.0
    trade_funding: float = 0.0
    entry_breakdown: dict[str, float] | None = None
    entry_signal_z: float | None = None
    trades: list[dict[str, Any]] = field(default_factory=list)
    daily: list[dict[str, Any]] = field(default_factory=list)


def _pnl_at(ctx: SimContext, state: _SimState, cur_near: float, cur_far: float) -> float:
    if state.position == 0:
        return 0.0
    diff = (cur_near - state.entry_near) * ctx.near_multiplier - (
        cur_far - state.entry_far
    ) * ctx.far_multiplier
    return ctx.params.quantity * state.position * diff


def _close_trade(
    ctx: SimContext, state: _SimState, date_key: str, cur_near: float, cur_far: float, reason: str
) -> None:
    direction = "long_near_short_far" if state.position > 0 else "short_near_long_far"
    entry_sp = _round(state.entry_near * ctx.near_multiplier - state.entry_far * ctx.far_multiplier)
    exit_sp = _round(cur_near * ctx.near_multiplier - cur_far * ctx.far_multiplier)
    sig_z = _round(state.entry_signal_z) if state.entry_signal_z is not None else None
    if ctx.fast_intents:
        state.trades.append(
            {
                "entry_date": state.entry_date,
                "exit_date": date_key,
                "direction": direction,
                "quantity": ctx.params.quantity,
                "entry_near_fill": state.entry_near,
                "entry_far_fill": state.entry_far,
                "exit_near_fill": cur_near,
                "exit_far_fill": cur_far,
                "entry_spread_fill": entry_sp,
                "exit_spread_fill": exit_sp,
                "entry_signal_z": sig_z,
                "exit_reason": reason,
            }
        )
    else:
        _append_priced_trade(
            ctx,
            state,
            date_key,
            (cur_near, cur_far),
            reason,
            (direction, entry_sp, exit_sp, sig_z),
        )
    state.position = 0
    state.entry_date = None
    state.trade_funding = 0.0
    state.entry_breakdown = None
    state.entry_signal_z = None


def _exit_side_cost(
    ctx: SimContext, cur_legs: tuple[float, float], date_key: str
) -> dict[str, float] | None:
    """Tariff cost charged on the closing fill, or ``None`` when tariffs are off."""
    if not (ctx.tariff_costs and ctx.normalized_cost is not None):
        return None
    cur_near, cur_far = cur_legs
    return side_cost(
        ctx.normalized_cost,
        [(ctx.near_contract, cur_near), (ctx.far_contract, cur_far)],
        ctx.params.quantity,
        as_of=date_key,
    )


def _round_trip_cost(
    ctx: SimContext,
    state: _SimState,
    date_key: str,
    exit_bd: dict[str, float] | None,
) -> dict[str, float] | None:
    """Full round-trip cost breakdown: entry side already paid, plus the exit side."""
    if ctx.tariff_costs and state.entry_breakdown is not None and exit_bd is not None:
        return {key: state.entry_breakdown[key] + exit_bd[key] for key in exit_bd}
    if ctx.normalized_cost is None:
        return None
    return _trade_cost_breakdown(
        ctx.normalized_cost,
        entry_timestamp=state.entry_date or date_key,
        exit_timestamp=date_key,
        quantity=ctx.params.quantity,
        near_contract=ctx.near_contract,
        far_contract=ctx.far_contract,
    )


def _append_priced_trade(
    ctx: SimContext,
    state: _SimState,
    date_key: str,
    cur_legs: tuple[float, float],
    reason: str,
    meta: tuple[str, float, float, float | None],
) -> None:
    cur_near, cur_far = cur_legs
    direction, entry_sp, exit_sp, sig_z = meta
    price_gross = _pnl_at(ctx, state, cur_near, cur_far)
    gross = price_gross + state.trade_funding
    exit_bd = _exit_side_cost(ctx, cur_legs, date_key)
    breakdown = _round_trip_cost(ctx, state, date_key, exit_bd)
    cost = sum(breakdown.values()) if breakdown is not None else 0.0
    state.realized_gross += price_gross
    state.realized_costs += sum(exit_bd.values()) if exit_bd is not None else cost
    if breakdown is not None:
        for component, value in (exit_bd or breakdown).items():
            state.cost_totals[component] += value
    tick_size = float(ctx.near_contract["tick_size"])
    state.trades.append(
        {
            "entry_date": state.entry_date,
            "exit_date": date_key,
            "direction": direction,
            "quantity": ctx.params.quantity,
            "entry_spread_ticks": _round((state.entry_near - state.entry_far) / tick_size),
            "exit_spread_ticks": _round((cur_near - cur_far) / tick_size),
            "entry_near_fill": state.entry_near,
            "entry_far_fill": state.entry_far,
            "exit_near_fill": cur_near,
            "exit_far_fill": cur_far,
            "entry_spread_fill": entry_sp,
            "exit_spread_fill": exit_sp,
            "entry_signal_z": sig_z,
            "gross_pnl": _round(gross),
            "price_gross_pnl": _round(price_gross),
            "funding_rub": _round(state.trade_funding),
            "modeled_costs": _round(cost) if breakdown is not None else None,
            "cost_breakdown": breakdown,
            "entry_costs": {k: _round(v) for k, v in state.entry_breakdown.items()}
            if state.entry_breakdown is not None
            else None,
            "exit_costs": {k: _round(v) for k, v in exit_bd.items()}
            if exit_bd is not None
            else None,
            "net_pnl": _round(gross - cost) if breakdown is not None else None,
            "exit_reason": reason,
        }
    )


def _row_signal_z(
    index: int, params: BaselineParams, spread_ticks: list[float] | None
) -> float | None:
    signal_index = index - params.decision_delay_bars
    if signal_index < params.lookback:
        return None
    if params.precomputed_z is not None:
        return params.precomputed_z[index]
    assert spread_ticks is not None
    window = spread_ticks[signal_index - params.lookback : signal_index]
    mean, std = _rolling_stats(window)
    return (spread_ticks[signal_index] - mean) / std if std > 0 else 0.0


def _open_position(
    ctx: SimContext,
    state: _SimState,
    row: dict[str, Any],
    fill_near: float,
    fill_far: float,
    signal_z: float,
) -> str:
    state.position = 1 if signal_z < 0 else -1
    state.entry_near, state.entry_far = fill_near, fill_far
    state.entry_date = row.get("timestamp", row["date"])
    state.entry_signal_z = signal_z
    if ctx.tariff_costs and ctx.normalized_cost is not None:
        entry_bd = side_cost(
            ctx.normalized_cost,
            [(ctx.near_contract, fill_near), (ctx.far_contract, fill_far)],
            ctx.params.quantity,
            as_of=str(state.entry_date),
        )
        state.entry_breakdown = entry_bd
        state.realized_costs += sum(entry_bd.values())
        for component, value in entry_bd.items():
            state.cost_totals[component] += value
    return "enter_long_spread" if state.position > 0 else "enter_short_spread"


def _step_row_decision(
    ctx: SimContext,
    state: _SimState,
    row: dict[str, Any],
    index: int,
    row_count: int,
    signal_z: float | None,
) -> str:
    action = "hold_flat" if state.position == 0 else "hold_position"
    if signal_z is None:
        return action
    fill_near, fill_far = (
        (float(row["near_open"]), float(row["far_open"]))
        if ctx.params.fill == "open"
        else (float(row["near_close"]), float(row["far_close"]))
    )
    if state.position != 0 and abs(signal_z) <= ctx.params.exit_z:
        _close_trade(
            ctx,
            state,
            row.get("timestamp", row["date"]),
            fill_near,
            fill_far,
            "mean_reversion_exit",
        )
        return "exit"
    if state.position == 0 and index < row_count - 1 and abs(signal_z) >= ctx.params.entry_z:
        return _open_position(ctx, state, row, fill_near, fill_far, signal_z)
    return action


def _finalize_open_end_of_sample(ctx: SimContext, state: _SimState, last: dict[str, Any]) -> None:
    if state.position == 0:
        return
    _close_trade(
        ctx,
        state,
        last.get("timestamp", last["date"]),
        float(last["near_close"]),
        float(last["far_close"]),
        "end_of_sample",
    )
    if ctx.fast_intents:
        if ctx.params.funding_swaprates:
            state.daily[-1]["action"] = "forced_exit_end_of_sample"
            state.daily[-1]["position"] = 0
        return
    state.daily[-1]["action"] = "forced_exit_end_of_sample"
    state.daily[-1]["position"] = 0
    state.daily[-1]["strategy_value"] = _round(
        ctx.params.initial_capital
        + state.realized_gross
        + state.cumulative_funding
        - state.realized_costs
    )
    state.daily[-1]["X"] = _round(
        state.daily[-1]["strategy_value"] - state.daily[-1]["benchmark_value"]
    )
    state.daily[-1]["gross_realized_pnl"] = _round(state.realized_gross + state.cumulative_funding)
    state.daily[-1]["modeled_costs"] = (
        _round(state.realized_costs) if ctx.normalized_cost is not None else None
    )


def _apply_row_funding(ctx: SimContext, state: _SimState, date_key: str, lot_size: float) -> float:
    if not (ctx.params.funding_swaprates and state.position):
        return 0.0
    swaprate = ctx.params.funding_swaprates.get(date_key)
    if swaprate is None:
        return 0.0
    funding_today = -state.position * float(swaprate) * lot_size * ctx.params.quantity
    if not ctx.fast_intents:
        state.trade_funding += funding_today
        state.cumulative_funding += funding_today
    return funding_today


def run_mean_reversion_baseline(series: dict[str, Any], **options: Any) -> dict[str, Any]:
    """Run the delayed z-score control."""
    params = parse_baseline_params(options)
    ctx = build_sim_context(series, params)
    rows = series.get("rows", [])
    lot_size = float(ctx.near_contract["lot_size"])
    fast_res = try_fast_intents_scan(
        rows, params, ctx.near_multiplier, ctx.far_multiplier, lot_size
    )
    if fast_res is not None:
        return fast_res
    benchmark_start = float(rows[0]["benchmark_price"])
    spread_ticks = (
        None if params.precomputed_z is not None else [float(item["spread_ticks"]) for item in rows]
    )
    state = _SimState(cost_totals=dict.fromkeys(ctx.cost_components, 0.0))
    row_count = len(rows)
    for index, row in enumerate(rows):
        signal_z = _row_signal_z(index, params, spread_ticks)
        action = _step_row_decision(ctx, state, row, index, row_count, signal_z)
        funding_today = _apply_row_funding(ctx, state, str(row["date"]), lot_size)
        unrealized = (
            0.0
            if ctx.fast_intents
            else _pnl_at(ctx, state, float(row["near_close"]), float(row["far_close"]))
        )
        record_daily_step(
            ctx, state, row, action, signal_z, funding_today, (benchmark_start, unrealized)
        )
    _finalize_open_end_of_sample(ctx, state, rows[-1])
    if ctx.fast_intents:
        return {"trades": state.trades, "daily": state.daily}
    return assemble_baseline_report(series, rows, ctx, state)


def assemble_baseline_report(
    series: dict[str, Any],
    rows: list[dict[str, Any]],
    ctx: Any,
    state: Any,
) -> dict[str, Any]:
    params = ctx.params
    overlay = None
    if params.cash_in_lqdt:
        overlay = lqdt_overlay_values(
            params.initial_capital,
            [row["strategy_value"] - params.initial_capital for row in state.daily],
            [
                ctx.margin_per_pair * params.quantity if row["position"] else 0.0
                for row in state.daily
            ],
            [float(row["benchmark_price"]) for row in rows],
        )
        for row, value in zip(state.daily, overlay.values, strict=True):
            row["strategy_value"] = _round(value)
            row["X"] = _round(value - row["benchmark_value"])
    peak = 0.0
    for row in state.daily:
        peak = max(peak, float(row["X"]))
        row["H"] = _round(peak)
        row["D"] = _round(peak - float(row["X"]))
    final = state.daily[-1]
    net_status = (
        "usable_for_gross_only"
        if ctx.normalized_cost is None
        else "published_tariff_net"
        if ctx.tariff_costs
        else "illustrative_net_sensitivity"
    )
    return {
        "schema_version": ANALYTICS_SCHEMA,
        "release_state": "unit_signal_intents_not_portfolio"
        if params.sizing_intents
        else "exploratory_inconclusive",
        "near": series["near"],
        "far": series["far"],
        "direction": series.get("direction", "near_minus_far"),
        "analysis_role": params.analysis_role,
        "timeframe": series.get("timeframe", "1d"),
        "price_interpretation": "ISS candle close analytical proxy under A-06",
        "execution_evidence_status": "historical_bid_ask_unavailable",
        "parameters": {
            "initial_capital": params.initial_capital,
            "lookback": params.lookback,
            "entry_z": params.entry_z,
            "exit_z": params.exit_z,
            "quantity": params.quantity,
            "decision_delay_bars": params.decision_delay_bars,
            "fill": params.fill,
            "accounting": "free_cash_in_lqdt_margin_cash_idle"
            if params.cash_in_lqdt
            else "idle_cash_no_yield",
            "margin_per_pair": _round(ctx.margin_per_pair),
            "margin_gate": "deferred_to_funded_sizing" if params.sizing_intents else "passed",
        },
        "economics": {
            "price_unit": series.get("price_unit", "RUB_per_CNY"),
            "near_lot_size": ctx.near_contract["lot_size"],
            "far_lot_size": ctx.far_contract["lot_size"],
            "near_tick_size": ctx.near_contract["tick_size"],
            "far_tick_size": ctx.far_contract["tick_size"],
            "near_tick_value_rub": ctx.near_contract["tick_value"],
            "far_tick_value_rub": ctx.far_contract["tick_value"],
            "pair_initial_margin_rub": _round(ctx.margin_per_pair * params.quantity),
            "one_spread_tick_value_rub": _round(
                float(series.get("spread_tick_value_rub", ctx.near_contract["tick_value"]))
            ),
            "contract_terms_status": "current_snapshot_fallback_assumed",
        },
        "costs": {
            "status": ctx.cost_status,
            "components": list(ctx.cost_components),
            "profile": ctx.normalized_cost,
            "total_by_component": {k: _round(v) for k, v in state.cost_totals.items()}
            if ctx.normalized_cost is not None
            else None,
            "net_status": net_status,
        },
        "summary": {
            "common_row_count": len(rows),
            "first_date": rows[0]["date"],
            "last_date": rows[-1]["date"],
            "trade_count": len(state.trades),
            "gross_pnl": _round(state.realized_gross + state.cumulative_funding),
            "price_gross_pnl": _round(state.realized_gross),
            "funding_rub": _round(state.cumulative_funding),
            "modeled_costs": _round(state.realized_costs)
            if ctx.normalized_cost is not None
            else None,
            "costs_applied": ctx.normalized_cost is not None,
            "final_strategy_value": final["strategy_value"],
            "final_benchmark_value": final["benchmark_value"],
            "final_X": final["X"],
            "max_drawdown_D": _round(max(float(row["D"]) for row in state.daily)),
            "min_X": _round(min(float(row["X"]) for row in state.daily)),
            "benchmark_source_kinds": sorted({row["benchmark_source_kind"] for row in state.daily}),
            "benchmark_mark_kinds": sorted(
                {row.get("benchmark_mark_kind", "daily_close_same_date") for row in state.daily}
            ),
            "cash_shortfall_rows": overlay.shortfall_rows if overlay else 0,
        },
        "trades": state.trades,
        "daily": state.daily,
        "input_provenance": series["provenance"],
    }


def record_daily_step(
    ctx: Any,
    state: Any,
    row: dict[str, Any],
    action: str,
    signal_z: float | None,
    funding_today: float,
    vals: tuple[float, float],
) -> None:
    if ctx.fast_intents:
        if ctx.params.funding_swaprates:
            state.daily.append(
                {
                    "date": row["date"],
                    "action": action,
                    "position": state.position,
                    "funding_rub": _round(funding_today),
                }
            )
        return
    benchmark_start, unrealized_pnl = vals
    bench_val = ctx.params.initial_capital * float(row["benchmark_price"]) / benchmark_start
    strat_val = (
        ctx.params.initial_capital
        + state.realized_gross
        - state.realized_costs
        + state.cumulative_funding
        + unrealized_pnl
    )
    state.daily.append(
        {
            "date": row["date"],
            "timestamp": row.get("timestamp", row["date"]),
            "spread_ticks": row["spread_ticks"],
            "signal_z": _round(signal_z) if signal_z is not None else None,
            "action": action,
            "position": state.position,
            "strategy_value": _round(strat_val),
            "benchmark_value": _round(bench_val),
            "X": _round(strat_val - bench_val),
            "gross_realized_pnl": _round(state.realized_gross + state.cumulative_funding),
            "funding_rub": _round(funding_today),
            "cumulative_funding_rub": _round(state.cumulative_funding),
            "modeled_costs": _round(state.realized_costs)
            if ctx.normalized_cost is not None
            else None,
            "benchmark_source_kind": row["benchmark_source_kind"],
            "benchmark_mark_kind": row.get("benchmark_mark_kind", "daily_close_same_date"),
        }
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
    "_rolling_stats",
    "_round",
    "_trade_cost_breakdown",
    "_validate_cost_profile",
    "build_daily_series",
    "build_series",
    "compute_pair_entry_bands",
    "compute_rolling_z",
    "get_cost_scenario",
    "load_contracts",
    "run_mean_reversion_baseline",
]
