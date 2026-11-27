"""Continuous account and benchmark evaluation for proof search."""

from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Any

from moex_spread_lab.accounting.costs import lqdt_overlay_values
from moex_spread_lab.accounting.position_sizing import automatic
from moex_spread_lab.core.settings import DEFAULT_INITIAL_CAPITAL_RUB, EvaluationSplit
from moex_spread_lab.core.w0 import W0Error


def excess_curve(proof: dict[str, Any], marks: dict[str, float]) -> dict[str, float]:
    """Daily proof ledger minus the accrued LQDT hurdle on each open trade's entry margin.

    Allocate the *published* hurdle exactly at exit, including rounding. Unlike using
    realized trades alone this marks positions crossing a split and intra-trade losses.
    """
    ledger: list[dict[str, Any]] = proof["ledger"]
    if automatic(proof.get("selection", {})):
        initial: float = proof["sizing"]["initial_capital_rub"]
        start: float = marks[ledger[0]["date"]]
        return {
            r["date"]: r["account_value_rub"] - initial * marks[r["date"]] / start for r in ledger
        }
    index = {row["date"]: i for i, row in enumerate(ledger)}
    hurdle_changes = [0.0] * len(ledger)
    trades: list[dict[str, Any]] = proof["trades"]
    for trade in trades:
        entry, exit_ = trade["entry"]["date"], trade["exit"]["date"]
        previous = 0.0
        for i in range(index[entry] + 1, index[exit_] + 1):
            accrued = (
                trade["lqdt_on_margin_rub"]
                if i == index[exit_]
                else trade["margin_rub"] * (marks[ledger[i]["date"]] / marks[entry] - 1)
            )
            hurdle_changes[i] += accrued - previous
            previous = accrued
    hurdle = 0.0
    curve: dict[str, float] = {}
    for row, delta in zip(ledger, hurdle_changes, strict=True):
        hurdle += delta
        curve[row["date"]] = row["cumulative_net_rub"] - hurdle
    return curve


def account_checks(proof: dict[str, Any]) -> dict[str, Any]:
    """Descriptive cash-path evidence; it never waives a failed annualization/ranking gate."""
    rows: list[dict[str, Any]] = proof["ledger"]
    failed = [r for r in rows if r["cash_shortfall"]]
    return {
        "cash_shortfall_rows": len(failed),
        "first_cash_shortfall_date": failed[0]["date"] if failed else None,
        "minimum_free_cash_rub": round(min(r["free_cash_rub"] for r in rows), 2),
    }


def _held_margins(
    rows: list[dict[str, Any]], idx: dict[str, int], trades: list[dict[str, Any]]
) -> list[float]:
    changes = [0.0] * len(rows)
    for t in trades:
        # Entry margin is posted after entry and released at exit; account wealth
        # itself never resets at the boundary. Include zero-volume days while held.
        a, b = t["entry"]["date"], t["exit"]["date"]
        if a in idx:
            changes[idx[a]] += t["margin_rub"]
        if b in idx:
            changes[idx[b]] -= t["margin_rub"]
    margin = 0.0
    margins: list[float] = []
    for delta in changes:
        margin += delta
        margins.append(margin)
    return margins


def as_datetime(value: str) -> datetime:
    """Parse an ISO timestamp or bare ISO date (UTC ``Z`` suffix tolerated)."""
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return datetime.combine(date.fromisoformat(value), time.min)


def _growth(ratio: float, power: float) -> float:
    return 100 * math.expm1(power * math.log(ratio))


def annualize(
    opening: float, closing: float, anchor_price: float, end_price: float, start: str, end: str
) -> dict[str, float] | str:
    """Annualized strategy and LQDT returns, or the reason no finite estimate exists."""
    days = (as_datetime(end) - as_datetime(start)).total_seconds() / 86400
    if days <= 0 or min(opening, closing) <= 0:
        return "Нет положительного капитала или календарного интервала для годовой доходности."
    try:
        strategy = _growth(closing / opening, 365 / days)
        benchmark = _growth(end_price / anchor_price, 365 / days)
    except (OverflowError, ValueError):
        strategy = benchmark = math.inf
    excess = strategy - benchmark
    if not math.isfinite(excess):
        return "Годовая оценка не является конечным числом."
    return {
        "strategy_annualized_pct": strategy,
        "lqdt_annualized_pct": benchmark,
        "annualized_excess_pp": excess if abs(excess) >= 1e-10 else 0.0,
        "calendar_days": round(days, 4),
    }


def _wealth_path(
    proof: dict[str, Any],
    rows: list[dict[str, Any]],
    idx: dict[str, int],
    initial: float,
    prices: list[float],
) -> tuple[list[float], bool]:
    if automatic(proof.get("selection", {})):
        return [r["account_value_rub"] for r in rows], any(r["cash_shortfall"] for r in rows)
    margins = _held_margins(rows, idx, proof["trades"])
    cumulative_net = [r["cumulative_net_rub"] for r in rows]
    values, shortfalls = lqdt_overlay_values(initial, cumulative_net, margins, prices)
    return values, bool(shortfalls)


def annualized_returns(
    proof: dict[str, Any], marks: dict[str, float], split: EvaluationSplit
) -> dict[str, Any]:
    """Two independently annualized wealth returns on the same dates, in percentage points."""
    initial: float = proof.get("sizing", {}).get(
        "initial_capital_rub", proof.get("initial_capital_rub", DEFAULT_INITIAL_CAPITAL_RUB)
    )
    start, train_end = split.training[0], split.training[-1]
    end = split.validation[-1] if split.validation else train_end
    ledger: list[dict[str, Any]] = proof["ledger"]
    rows = [r for r in ledger if start <= r["date"] <= end]
    idx = {r["date"]: i for i, r in enumerate(rows)}
    prices = [marks[r["date"]] for r in rows]
    if not all(math.isfinite(v) and v > 0 for v in prices):
        raise W0Error("Непригодная отметка LQDT в расчётном периоде.")
    values, shortfalls = _wealth_path(proof, rows, idx, initial, prices)
    anchor = train_end if split.validation else start
    opening = values[idx[anchor]] if split.validation else initial
    result = annualize(opening, values[-1], prices[idx[anchor]], prices[-1], anchor, end)
    if isinstance(result, str):
        raise W0Error(result)
    return {**result, "cash_shortfall": shortfalls}


def missing_exposure(
    proof: dict[str, Any],
    segments: list[tuple[str, tuple[str, ...]]],
    split: EvaluationSplit,
) -> tuple[list[str], list[str]]:
    trades: list[dict[str, Any]] = proof["trades"]
    missing = [
        label
        for label, days in segments
        if not any(t["entry"]["date"] <= days[-1] and t["exit"]["date"] >= days[0] for t in trades)
    ]
    reasons: list[str] = []
    if missing:
        where = (
            "ни на обучении, ни на проверке"
            if len(missing) == 2
            else f"только {missing[0]}"
            if split.validation
            else missing[0]
        )
        reasons.append(f"Открытого спреда не было {where}.")
    return missing, reasons
