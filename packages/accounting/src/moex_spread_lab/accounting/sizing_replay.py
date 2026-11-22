"""Causal bar-by-bar Python position sizing replay engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from moex_spread_lab.core.w0 import W0Error

from .sizing_report import ReplayState, finalize_sized_proof
from .sizing_terms import AUTO_SIZING, _money, _positive, _Terms, maximum_quantity


@dataclass(frozen=True)
class _ReplayContext:
    terms: _Terms
    settings: Any
    prices: list[float]
    carry_settlement: bool
    metrics_only: bool


@dataclass(frozen=True)
class _RowInputs:
    index: int
    row: dict[str, Any]
    eligible: bool
    entry_eligible: bool
    exit_eligible: bool
    events: list[str] | None


def _scaled_costs(costs: dict[str, float], quantity: int) -> dict[str, float]:
    return {key: _money(val * quantity) for key, val in costs.items()}


def _move_position(pos: dict[str, Any], end_mark: float, point: float) -> float:
    change = _money(pos["sign"] * (end_mark - pos["mark"]) * point * pos["trade"]["quantity"])
    pos["trade"]["_gross"] += change
    pos["mark"] = end_mark
    return change


def _is_carry_settlement(strategy_name: str, proof: dict[str, Any]) -> bool:
    return (
        strategy_name == "funding_only" or proof.get("accounting_policy") == "carry_settlement.v1"
    )


def _close_open_trade(
    trade: dict[str, Any],
    index: int,
    ctx: _ReplayContext,
    *,
    at_open: bool,
    events: list[str] | None,
) -> float:
    _, unit_costs = ctx.terms.at_fill(trade["exit"], index, at_open=at_open)
    is_carry_open_end = bool(trade.get("open_at_end")) and ctx.carry_settlement
    trade["exit"]["costs"] = (
        {} if is_carry_open_end else _scaled_costs(unit_costs, trade["quantity"])
    )
    if events is not None:
        events.append(f"EXIT #{trade['number']}")
    return sum(trade["exit"]["costs"].values())


def _step_open_position(
    state: ReplayState,
    ctx: _ReplayContext,
    inp: _RowInputs,
) -> tuple[float, float, float, dict[str, Any] | None]:
    if state.position is None:
        return 0.0, 0.0, state.wealth, None
    trade = state.position["trade"]
    open_at_end = bool(trade.get("open_at_end"))
    exiting = inp.row["date"] == trade["exit"]["date"]
    should_exit = exiting and (inp.exit_eligible or open_at_end)
    if not should_exit and not (inp.eligible and not exiting):
        return 0.0, 0.0, state.wealth, trade

    end_mark = trade["exit"]["spread_fill"] if should_exit else inp.row["spread"]
    at_open = should_exit and ctx.terms.open_fill and not open_at_end
    point = (
        ctx.terms.fill_point(inp.index, at_open=at_open)
        if should_exit
        else ctx.terms.point(inp.index)
    )
    change = _move_position(state.position, end_mark, point)
    if not should_exit:
        return change, 0.0, state.wealth, trade

    fee = _close_open_trade(
        trade, inp.index, ctx, at_open=ctx.terms.open_fill and not open_at_end, events=inp.events
    )
    state.position = None
    funding_owner = trade if inp.row.get("funding_contracts", 0) else None
    return change, fee, state.wealth + change - fee, funding_owner


def _record_skipped_entry(
    state: ReplayState,
    day: str,
    budget: float,
    margin: float,
    candidate: dict[str, Any],
    events: list[str] | None,
) -> None:
    if events is None:
        return
    why = (
        "Недостаточно свободных средств для маржи; новые входы остановлены."
        if state.paused
        else "Недостаточно средств на один спред, издержки и обязательный резерв."
    )
    state.rejected.append(
        {
            "date": day,
            "quantity": 0,
            "reason": why,
            "available_capital_rub": _money(budget),
            "margin_per_pair_rub": _money(margin),
            "signal": candidate.get("signal"),
        }
    )
    events.append("SKIP: " + why)


def _open_accepted_candidate(
    state: ReplayState,
    ctx: _ReplayContext,
    candidate: dict[str, Any],
    qty: int,
    budget: float,
    margin_and_costs: tuple[float, dict[str, float]],
    events: list[str] | None,
) -> float:
    margin, entry_costs = margin_and_costs
    candidate.update(
        number=len(state.accepted) + 1,
        quantity=qty,
        margin_rub=_money(margin * qty),
        _gross=0.0,
        _funding=0.0,
    )
    if not ctx.metrics_only:
        candidate["sizing"] = {
            "mode": AUTO_SIZING,
            "available_capital_rub": _money(budget),
            "margin_per_pair_rub": margin,
            "entry_cost_per_pair_rub": sum(entry_costs.values()),
            "stress_reserve_enforced": not ctx.settings.allow_failed_capital,
        }
        candidate["entry"]["margin_rub"] = candidate["margin_rub"]
    candidate["entry"]["costs"] = _scaled_costs(entry_costs, qty)
    state.accepted.append(candidate)
    if events is not None:
        events.append(f"ENTRY #{candidate['number']}")
    sign = 1 if candidate["side"] == "long_spread" else -1
    state.position = {"trade": candidate, "sign": sign, "mark": candidate["entry"]["spread_fill"]}
    return sum(candidate["entry"]["costs"].values())


def _step_candidate_entry(
    state: ReplayState,
    ctx: _ReplayContext,
    inp: _RowInputs,
    candidate: dict[str, Any] | None,
    fill_wealth: float,
    prior_funding_owner: dict[str, Any] | None,
) -> tuple[float, float, dict[str, Any] | None]:
    if candidate is None or not inp.entry_eligible:
        return 0.0, 0.0, prior_funding_owner
    if state.position is not None:
        raise W0Error("Перекрывающиеся сигналы не поддерживаются одним счётом окна.")
    day = inp.row["date"]
    margin, entry_costs = ctx.terms.at_fill(
        candidate["entry"], inp.index, at_open=ctx.terms.open_fill
    )
    budget = max(0.0, min(state.initial, fill_wealth))
    qty = (
        0
        if state.paused
        else maximum_quantity(
            budget,
            margin,
            entry_costs,
            stress=not ctx.settings.allow_failed_capital,
            native_spread=ctx.terms.native_order,
        )
    )
    if not qty:
        _record_skipped_entry(state, day, budget, margin, candidate, inp.events)
        return 0.0, 0.0, prior_funding_owner

    cost = _open_accepted_candidate(
        state, ctx, candidate, qty, budget, (margin, entry_costs), inp.events
    )
    assert state.position is not None
    same_day_exit = candidate["exit"]["date"] == day
    end_mark = candidate["exit"]["spread_fill"] if same_day_exit else inp.row["spread"]
    daily = (
        _move_position(state.position, end_mark, ctx.terms.point(inp.index))
        if inp.eligible
        else 0.0
    )
    if same_day_exit:
        cost += _close_open_trade(candidate, inp.index, ctx, at_open=False, events=inp.events)
        state.position = None
    return daily, cost, candidate


def _append_row_ledger(
    state: ReplayState,
    ctx: _ReplayContext,
    inp: _RowInputs,
    daily_raw: float,
    cost_raw: float,
    funding_owner: dict[str, Any] | None,
) -> None:
    row = inp.row
    idx = inp.index
    day = row["date"]
    unit_funding = row.get("funding_rub")
    unit_funding_contracts = row.get("funding_contracts", 0)
    funding_q = (
        funding_owner["quantity"] if funding_owner is not None and unit_funding_contracts else 0
    )
    funding = _money(float(unit_funding or 0.0) * funding_q)
    if funding_owner is not None:
        funding_owner["_gross"] += funding
        funding_owner["_funding"] += funding
    daily = _money(daily_raw + funding)
    cost = _money(cost_raw)
    state.cumulative = _money(state.cumulative + daily - cost)
    growth = ctx.prices[idx] / ctx.prices[idx - 1] if idx else 1.0
    state.wealth = state.free * growth + state.posted + daily - cost
    if state.position is None:
        state.posted = 0.0
    elif inp.eligible or inp.entry_eligible:
        state.posted = ctx.terms.posted_margin(idx, state.position["trade"]["quantity"])
    state.free = state.wealth - state.posted
    shortfall = state.free < -1e-7
    state.shortfalls += int(shortfall)
    excess = state.wealth - state.initial * ctx.prices[idx] / ctx.prices[0]
    state.peak = max(state.peak, excess)
    state.drawdown = max(state.drawdown, state.peak - excess)
    state.paused |= shortfall
    if ctx.metrics_only:
        state.ledger.append(
            {
                "date": day,
                "cumulative_net_rub": state.cumulative,
                "account_value_rub": state.wealth,
                "cash_shortfall": shortfall,
            }
        )
        return
    q_held = state.position["trade"]["quantity"] if state.position is not None else 0
    row.update(
        quantity=q_held,
        margin_rub=_money(state.posted),
        funding_rub=funding if unit_funding is not None else None,
    )
    if "funding_contracts" in row:
        row["funding_contracts"] = funding_q
    state.ledger.append(
        {
            "date": day,
            "timestamp": row.get("timestamp", day),
            "session_date": row.get("session_date", day[:10]),
            "benchmark_mark_date": row.get("benchmark_mark_date"),
            "position": state.position["sign"] if state.position else 0,
            "quantity": q_held,
            "mark_pnl_rub": daily,
            "costs_rub": cost,
            "funding_rub": funding if unit_funding is not None else None,
            "lqdt_on_margin_rub": None,
            "cumulative_net_rub": state.cumulative,
            "event": " + ".join(inp.events or ()) or None,
            "posted_margin_rub": state.posted,
            "free_cash_rub": state.free,
            "account_value_rub": state.wealth,
            "cash_shortfall": shortfall,
        }
    )


def replay_sizing_series(
    proof: dict[str, Any],
    marks: dict[str, float],
    settings: Any,
    terms: _Terms,
    initial: float,
    strategy_name: str,
    *,
    metrics_only: bool,
) -> ReplayState:
    rows = proof["series"]
    ctx = _ReplayContext(
        terms=terms,
        settings=settings,
        prices=[_positive(marks[r["date"]], "отметка LQDT") for r in rows],
        carry_settlement=_is_carry_settlement(strategy_name, proof),
        metrics_only=metrics_only,
    )
    intents = {t["entry"]["date"]: t for t in proof["trades"]}
    state = ReplayState(initial=initial, free=initial, wealth=initial)

    for idx, row in enumerate(rows):
        eligible = bool(row.get("volume_eligible", True))
        inp = _RowInputs(
            index=idx,
            row=row,
            eligible=eligible,
            entry_eligible=bool(row.get("entry_eligible", eligible)),
            exit_eligible=bool(row.get("exit_eligible", eligible)),
            events=None if metrics_only else [],
        )
        daily_exit, cost_exit, fill_wealth, funding_owner = _step_open_position(state, ctx, inp)
        daily_entry, cost_entry, funding_owner = _step_candidate_entry(
            state, ctx, inp, intents.get(row["date"]), fill_wealth, funding_owner
        )
        _append_row_ledger(
            state, ctx, inp, daily_exit + daily_entry, cost_exit + cost_entry, funding_owner
        )
    return state


__all__ = ["ReplayState", "finalize_sized_proof", "replay_sizing_series"]
