"""Frozen carry hypothesis simulation engine."""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.account_risk import assess_vtb_pair
from moex_spread_lab.core.w0 import load_contracts

from .strategy_costs import make_benchmark_lookup, resolve_carry_cost_profile
from .strategy_report import (
    CarryConfig,
    append_carry_daily_row,
    assemble_carry_report,
    parse_carry_config,
    resolve_expiry_windows,
)
from .study import CARRY_SCHEMA, CarryStrategyError


@dataclass
class _CarrySimCtx:
    cfg: CarryConfig
    rows: list[dict[str, Any]]
    entry_cost: float
    exit_cost: float
    expiry_cutoff_iso: str
    assignment_start_iso: str | None
    funding_vals: list[float]
    funding_known: list[str]
    basis_vals: list[float] | None


@dataclass
class _CarrySimState:
    position: bool = False
    entry_index: int | None = None
    entry_decision_date: str | None = None
    pending: dict[str, Any] | None = None
    cumulative_gross: float = 0.0
    cumulative_cost: float = 0.0
    trade_gross: float = 0.0
    trades: list[dict[str, Any]] = field(default_factory=list)
    daily: list[dict[str, Any]] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)


def _execute_order(
    ctx: _CarrySimCtx, state: _CarrySimState, order: dict[str, Any], index: int, date_key: str
) -> None:
    if order["action"] == "entry":
        if state.position:
            raise CarryStrategyError("entry attempted while a position is open")
        state.position = True
        state.entry_index = index
        state.entry_decision_date = order["decision_date"]
        state.trade_gross = 0.0
        state.cumulative_cost += ctx.entry_cost
        return
    if not state.position or state.entry_index is None or state.entry_decision_date is None:
        raise CarryStrategyError("exit attempted without an open position")
    state.cumulative_cost += ctx.exit_cost
    state.trades.append(
        {
            "entry_decision_date": state.entry_decision_date,
            "entry_execution_date": ctx.rows[state.entry_index]["date"],
            "exit_decision_date": order["decision_date"],
            "exit_execution_date": date_key,
            "status": "closed_proxy_round_trip",
            "gross_pnl": round(state.trade_gross, 8),
            "transaction_costs": ctx.entry_cost + ctx.exit_cost,
            "net_pnl": round(state.trade_gross - ctx.entry_cost - ctx.exit_cost, 8),
        }
    )
    state.position = False
    state.entry_index = None
    state.entry_decision_date = None


def _trailing_signals(ctx: _CarrySimCtx, index: int, date_key: str) -> tuple[float | None, float]:
    w_start = index - ctx.cfg.warmup_observations
    known_ok = all(ctx.funding_known[j] < date_key for j in range(w_start, index))
    trailing_funding = statistics.fmean(ctx.funding_vals[w_start:index]) if known_ok else None
    trailing_basis = (
        statistics.median(ctx.basis_vals[w_start:index]) if ctx.basis_vals is not None else 0.0
    )
    return trailing_funding, trailing_basis


def _decide_exit_reason(
    ctx: _CarrySimCtx,
    index: int,
    date_key: str,
    trailing_funding: float | None,
    trailing_basis: float,
) -> str | None:
    if ctx.assignment_start_iso is not None and date_key >= ctx.assignment_start_iso:
        return "perpetual_execution_window"
    if ctx.basis_vals is not None and ctx.basis_vals[index] >= trailing_basis:
        return "basis_reached_trailing_median"
    if trailing_funding is not None and trailing_funding <= 0:
        return "trailing_funding_non_positive"
    if date_key >= ctx.expiry_cutoff_iso:
        return "expiry_buffer"
    return None


def _handle_open_position_exit(
    ctx: _CarrySimCtx,
    state: _CarrySimState,
    index: int,
    date_key: str,
    exit_reason: str | None,
    increment: float,
    funding_included: float,
) -> tuple[float, float]:
    if not exit_reason or state.pending is not None:
        return increment, funding_included
    delay = ctx.cfg.execution_delay_rows
    available = index + delay < len(ctx.rows)
    if available:
        state.pending = {
            "action": "exit",
            "execute_index": index + delay,
            "decision_date": date_key,
            "reason": exit_reason,
        }
    state.signals.append(
        {
            "date": date_key,
            "action": "exit",
            "reason": exit_reason,
            "execution_available": available,
        }
    )
    if available and delay == 0 and state.pending is not None:
        funding_today = ctx.cfg.quantity * ctx.funding_vals[index]
        increment -= funding_today
        funding_included = 0.0
        state.cumulative_gross -= funding_today
        state.trade_gross -= funding_today
        _execute_order(ctx, state, state.pending, index, date_key)
        state.pending = None
    return increment, funding_included


def _handle_flat_position_entry(
    ctx: _CarrySimCtx,
    state: _CarrySimState,
    index: int,
    date_key: str,
    trailing_funding: float | None,
    trailing_basis: float,
) -> None:
    expiry_block = date_key >= ctx.expiry_cutoff_iso or (
        ctx.assignment_start_iso is not None and date_key >= ctx.assignment_start_iso
    )
    basis_ok = ctx.basis_vals is None or ctx.basis_vals[index] <= trailing_basis
    if (
        expiry_block
        or trailing_funding is None
        or trailing_funding <= 0
        or not basis_ok
        or state.pending is not None
    ):
        return
    delay = ctx.cfg.execution_delay_rows
    available = index + 1 < len(ctx.rows)
    reason = (
        "positive_trailing_funding"
        if ctx.cfg.strategy_variant == "funding_only"
        else "positive_trailing_funding_and_low_basis"
    )
    if available:
        state.pending = {
            "action": "entry",
            "execute_index": index + delay,
            "decision_date": date_key,
            "reason": reason,
        }
    state.signals.append(
        {"date": date_key, "action": "entry", "reason": reason, "execution_available": available}
    )
    if available and delay == 0 and state.pending is not None:
        _execute_order(ctx, state, state.pending, index, date_key)
        state.pending = None


def _simulate_carry_rows(
    ctx: _CarrySimCtx,
    benchmark_mark: Callable[[str], dict[str, Any]] | None,
    start_benchmark: dict[str, Any] | None,
) -> _CarrySimState:
    state = _CarrySimState()
    for index, row in enumerate(ctx.rows):
        date_key = str(row["date"])
        increment = 0.0
        funding_included = 0.0
        if state.position and state.entry_index is not None and index > state.entry_index:
            increment = ctx.cfg.quantity * float(row["daily_gross_carry_pnl"])
            funding_included = ctx.cfg.quantity * ctx.funding_vals[index]
            state.cumulative_gross += increment
            state.trade_gross += increment
        if state.pending is not None and state.pending["execute_index"] == index:
            _execute_order(ctx, state, state.pending, index, date_key)
            state.pending = None
        if index >= ctx.cfg.warmup_observations:
            tr_f, tr_b = _trailing_signals(ctx, index, date_key)
            if state.position:
                reason = _decide_exit_reason(ctx, index, date_key, tr_f, tr_b)
                increment, funding_included = _handle_open_position_exit(
                    ctx, state, index, date_key, reason, increment, funding_included
                )
            else:
                _handle_flat_position_entry(ctx, state, index, date_key, tr_f, tr_b)
        append_carry_daily_row(
            ctx,
            state,
            row,
            index,
            date_key,
            (increment, funding_included),
            (benchmark_mark, start_benchmark),
        )
    if state.position and state.entry_index is not None and state.entry_decision_date is not None:
        state.trades.append(
            {
                "entry_decision_date": state.entry_decision_date,
                "entry_execution_date": ctx.rows[state.entry_index]["date"],
                "exit_decision_date": None,
                "exit_execution_date": None,
                "status": "open_at_data_end_no_executable_exit",
                "gross_pnl": round(state.trade_gross, 8),
                "transaction_costs": ctx.entry_cost,
                "net_pnl": round(state.trade_gross - ctx.entry_cost, 8),
            }
        )
    return state


def build_carry_strategy(
    carry_result: dict[str, Any],
    benchmark_path: Path,
    contracts_path: Path,
    **options: Any,
) -> dict[str, Any]:
    """Evaluate a frozen carry hypothesis on settlement proxies."""
    cfg = parse_carry_config(options)
    if carry_result.get("schema_version") != CARRY_SCHEMA:
        raise CarryStrategyError("unsupported carry decomposition schema")
    rows = carry_result.get("rows")
    if not isinstance(rows, list) or len(rows) <= cfg.warmup_observations:
        raise CarryStrategyError("carry decomposition does not contain the required warmup")
    near, far = carry_result.get("near"), carry_result.get("far")
    if not isinstance(near, str) or not isinstance(far, str):
        raise CarryStrategyError("carry contract identities are required")
    contracts, contracts_hash = load_contracts(contracts_path, near, far)
    far_expiry = contracts[far].get("expiry")
    expiry_cutoff_iso, assignment_start_iso, assignment_status = resolve_expiry_windows(
        rows, far, far_expiry, cfg.expiry_buffer_days, cfg.assignment_window_trading_days
    )
    cost_profile = resolve_carry_cost_profile(
        cfg.transaction_cost_scenario,
        cfg.slippage_ticks,
        contracts[near],
        contracts[far],
        rows[-1],
    )
    entry_cost = float(cost_profile["entry_cost"]) * cfg.quantity
    exit_cost = float(cost_profile["exit_cost"]) * cfg.quantity
    account_risk = (
        None
        if cfg.metrics_only
        else assess_vtb_pair(
            contracts_path,
            near,
            far,
            initial_capital=cfg.initial_capital,
            observed_daily_pnl=[float(row["daily_gross_carry_pnl"]) for row in rows[1:]],
            quantity=cfg.quantity,
        )
    )
    benchmark_mark, start_benchmark, benchmark_hash = (
        (None, None, "")
        if cfg.metrics_only
        else make_benchmark_lookup(benchmark_path, str(rows[0]["date"]))
    )
    ctx = _CarrySimCtx(
        cfg=cfg,
        rows=rows,
        entry_cost=entry_cost,
        exit_cost=exit_cost,
        expiry_cutoff_iso=expiry_cutoff_iso,
        assignment_start_iso=assignment_start_iso,
        funding_vals=[float(row["daily_funding_received_short_near"]) for row in rows],
        funding_known=[str(row.get("funding_known_on", "")) for row in rows],
        basis_vals=[float(row["basis_far_minus_near"]) for row in rows]
        if cfg.strategy_variant == "basis_median"
        else None,
    )
    state = _simulate_carry_rows(ctx, benchmark_mark, start_benchmark)
    if cfg.metrics_only:
        return {"trades": state.trades, "signals": state.signals, "daily": state.daily}
    return assemble_carry_report(
        ctx,
        state,
        {
            "near": near,
            "far": far,
            "far_expiry": far_expiry,
            "assignment_status": assignment_status,
            "contracts": contracts,
            "contracts_hash": contracts_hash,
            "contracts_path": contracts_path,
            "benchmark_path": benchmark_path,
            "benchmark_hash": benchmark_hash,
            "benchmark_mark": benchmark_mark,
            "cost_profile": cost_profile,
            "account_risk": account_risk,
        },
    )
