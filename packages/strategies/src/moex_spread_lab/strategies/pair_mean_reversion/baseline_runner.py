"""Stateful simulation helpers for ordinary pair mean reversion."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from moex_spread_lab.accounting.costs import side_cost

from ..base import population_rolling_stats as _rolling_stats
from .costs_helper import (
    SimContext,
    build_sim_context,
)
from .costs_helper import (
    round_value as _round,
)
from .costs_helper import (
    trade_cost_breakdown as _trade_cost_breakdown,
)
from .fast_scan import (
    BaselineParams,
    compute_rolling_z,
    parse_baseline_params,
    try_fast_intents_scan,
)
from .report_builder import assemble_baseline_report, record_daily_step


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
    exit_bd = (
        side_cost(
            ctx.normalized_cost,
            [(ctx.near_contract, cur_near), (ctx.far_contract, cur_far)],
            ctx.params.quantity,
        )
        if ctx.tariff_costs and ctx.normalized_cost is not None
        else None
    )
    if ctx.tariff_costs and state.entry_breakdown is not None and exit_bd is not None:
        breakdown: dict[str, float] | None = {
            key: state.entry_breakdown[key] + exit_bd[key] for key in exit_bd
        }
    elif ctx.normalized_cost is not None:
        breakdown = _trade_cost_breakdown(
            ctx.normalized_cost,
            entry_timestamp=state.entry_date or date_key,
            exit_timestamp=date_key,
            quantity=ctx.params.quantity,
            near_contract=ctx.near_contract,
            far_contract=ctx.far_contract,
        )
    else:
        breakdown = None
    cost = sum(breakdown.values()) if breakdown is not None else 0.0
    state.realized_gross += price_gross
    state.realized_costs += (
        sum(exit_bd.values()) if (ctx.tariff_costs and exit_bd is not None) else cost
    )
    if breakdown is not None:
        inc_map = exit_bd if (ctx.tariff_costs and exit_bd is not None) else breakdown
        for component, value in inc_map.items():
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


__all__ = ["compute_rolling_z", "run_mean_reversion_baseline"]
