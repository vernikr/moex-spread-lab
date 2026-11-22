"""Continuous account and benchmark evaluation for proof search."""

from __future__ import annotations

import math
from datetime import date, datetime, time

from moex_spread_lab.accounting.portfolio import lqdt_overlay_values
from moex_spread_lab.accounting.position_sizing import automatic
from moex_spread_lab.core.evaluation_periods import DEFAULT_INITIAL_CAPITAL_RUB
from moex_spread_lab.core.w0 import W0Error


def _excess_curve(proof, marks):
    """Daily proof ledger minus the accrued LQDT hurdle on each open trade's entry margin.

    Allocate the *published* hurdle exactly at exit, including rounding. Unlike using
    realized trades alone this marks positions crossing a split and intra-trade losses.
    """
    ledger = proof["ledger"]
    if automatic(proof.get("selection", {})):
        initial = proof["sizing"]["initial_capital_rub"]
        start = marks[ledger[0]["date"]]
        return {
            r["date"]: r["account_value_rub"] - initial * marks[r["date"]] / start for r in ledger
        }
    index = {row["date"]: i for i, row in enumerate(ledger)}
    hurdle_changes = [0.0] * len(ledger)
    for trade in proof["trades"]:
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
    hurdle, curve = 0.0, {}
    for row, delta in zip(ledger, hurdle_changes, strict=True):
        hurdle += delta
        curve[row["date"]] = row["cumulative_net_rub"] - hurdle
    return curve


def account_checks(proof):
    """Descriptive cash-path evidence; it never waives a failed annualization/ranking gate."""
    rows = proof["ledger"]
    failed = [r for r in rows if r["cash_shortfall"]]
    return {
        "cash_shortfall_rows": len(failed),
        "first_cash_shortfall_date": failed[0]["date"] if failed else None,
        "minimum_free_cash_rub": round(min(r["free_cash_rub"] for r in rows), 2),
    }


def _held_margins(rows, idx, trades):
    changes = [0.0] * len(rows)
    for t in trades:
        # Entry margin is posted after entry and released at exit; account wealth
        # itself never resets at the boundary. Include zero-volume days while held.
        a, b = t["entry"]["date"], t["exit"]["date"]
        if a in idx:
            changes[idx[a]] += t["margin_rub"]
        if b in idx:
            changes[idx[b]] -= t["margin_rub"]
    margin, margins = 0.0, []
    for delta in changes:
        margin += delta
        margins.append(margin)
    return margins


def _annualized_returns(proof, marks, split):
    """Two independently annualized wealth returns on the same dates, in percentage points."""
    initial = proof.get("sizing", {}).get(
        "initial_capital_rub", proof.get("initial_capital_rub", DEFAULT_INITIAL_CAPITAL_RUB)
    )

    start, train_end = split.training[0], split.training[-1]
    end = split.validation[-1] if split.validation else train_end
    rows = [r for r in proof["ledger"] if start <= r["date"] <= end]
    idx = {r["date"]: i for i, r in enumerate(rows)}
    margins = _held_margins(rows, idx, proof["trades"])
    prices = [marks[r["date"]] for r in rows]
    if not all(math.isfinite(v) and v > 0 for v in prices):
        raise W0Error("Непригодная отметка LQDT в расчётном периоде.")
    if automatic(proof.get("selection", {})):
        values = [r["account_value_rub"] for r in rows]
        shortfalls = any(r["cash_shortfall"] for r in rows)
    else:
        values, shortfalls = lqdt_overlay_values(
            initial, [r["cumulative_net_rub"] for r in rows], margins, prices
        )
    anchor = train_end if split.validation else start

    def as_datetime(value):
        try:
            return datetime.fromisoformat(value.replace("Z", ""))
        except ValueError:
            return datetime.combine(date.fromisoformat(value), time.min)

    elapsed_days = (as_datetime(end) - as_datetime(anchor)).total_seconds() / 86400
    opening = values[idx[anchor]] if split.validation else initial
    if elapsed_days <= 0 or min(opening, values[-1]) <= 0:
        raise W0Error(
            "Нет положительного капитала или календарного интервала для годовой доходности."
        )
    power = 365 / elapsed_days
    try:
        strategy = 100 * math.expm1(power * math.log(values[-1] / opening))
        benchmark = 100 * math.expm1(power * math.log(prices[-1] / prices[idx[anchor]]))
        excess = strategy - benchmark
        if not all(math.isfinite(v) for v in (strategy, benchmark, excess)):
            raise OverflowError
    except (OverflowError, ValueError) as exc:
        raise W0Error("Годовая оценка не является конечным числом.") from exc
    return {
        "strategy_annualized_pct": strategy,
        "lqdt_annualized_pct": benchmark,
        "annualized_excess_pp": 0.0 if abs(excess) < 1e-10 else excess,
        "calendar_days": round(elapsed_days, 4),
        "cash_shortfall": bool(shortfalls),
    }


def _missing_exposure(proof, segments, split):
    missing = [
        label
        for label, days in segments
        if not any(
            t["entry"]["date"] <= days[-1] and t["exit"]["date"] >= days[0] for t in proof["trades"]
        )
    ]
    reasons = []
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
