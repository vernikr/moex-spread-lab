"""Settlement carry accounting for already validated position decisions.

Only the trusted carry study supplies prices and cash flows. A decision cannot
provide a price, funding rate, fee or P&L. This is an internal building block,
it is composed into the carry proof/search adapter.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping
from datetime import date, timedelta
from typing import cast

from moex_spread_lab.accounting.portfolio import lqdt_overlay_values
from moex_spread_lab.core.w0 import W0Error


def _validate_inputs(
    study, intents, quantity, side_cost, expiry_buffer_days, assignment_window_trading_days
):
    if (
        not isinstance(study, Mapping)
        or not isinstance(study.get("rows"), list)
        or not study["rows"]
        or not isinstance(intents, list)
        or type(quantity) is not int
        or quantity < 1
        or type(side_cost) not in (int, float)
        or not math.isfinite(side_cost)
        or side_cost < 0
        or type(expiry_buffer_days) is not int
        or expiry_buffer_days < 0
        or type(assignment_window_trading_days) is not int
        or assignment_window_trading_days < 1
    ):
        raise W0Error("Некорректные параметры учёта carry.")


def _validate_rows(rows):
    for row in rows:
        for key in (
            "daily_gross_carry_pnl",
            "daily_funding_received_short_near",
            "near_settle",
            "far_settle",
        ):
            if type(row[key]) not in (int, float) or not math.isfinite(row[key]):
                raise ValueError
        if row["near_settle"] <= 0 or row["far_settle"] <= 0:
            raise ValueError


def _validated_dates(study, far_expiry):
    rows = study["rows"]
    try:
        expiry = date.fromisoformat(far_expiry)
        dates = [date.fromisoformat(r["date"]) for r in rows]
        if dates != sorted(set(dates)):
            raise ValueError
        _validate_rows(rows)
    except (TypeError, KeyError, ValueError, OverflowError):
        raise W0Error("Некорректные даты или денежные данные carry.") from None
    return rows, expiry, dates


def _invalid_intent(trade, positions, horizon):
    e, x, sig, previous_exit, terminal, reason = positions
    dates, expiry, cutoff = horizon
    return (
        trade.get("side") != "short_spread"
        or type(terminal) is not bool
        or not isinstance(reason, str)
        or not reason
        or sig > e
        or e >= x
        or e <= previous_exit
        or dates[e] >= cutoff
        or dates[x] >= expiry
        or (terminal and (x != len(dates) - 1 or dates[x] >= cutoff))
        or (not terminal and dates[x] > cutoff and any(d >= cutoff for d in dates[e:x]))
    )


def _indexed_intents(rows, dates, expiry, cutoff, intents):
    by_day = {r["date"]: i for i, r in enumerate(rows)}
    entries, exits = {}, {}
    previous_exit = -1
    for trade in intents:
        if not isinstance(trade, Mapping):
            raise W0Error("Некорректная carry-сделка.")
        try:
            e, x, sig = (by_day[trade[k]] for k in ("entry_date", "exit_date", "signal_date"))
            terminal = trade["open_at_end"]
            reason = trade["reason"]
        except (KeyError, TypeError):
            raise W0Error("Неизвестная дата или поле carry-сделки.") from None
        if _invalid_intent(
            trade, (e, x, sig, previous_exit, terminal, reason), (dates, expiry, cutoff)
        ):
            raise W0Error("Carry-сделка нарушает порядок, экспирацию или окно поставки.")
        entries[e] = trade
        if not terminal:
            exits[x] = trade
        previous_exit = x
    return entries, exits


def _account_config(initial_capital, margin_per_pair, benchmark, rows):
    if (
        type(initial_capital) not in (int, float)
        or not math.isfinite(cast(float, initial_capital))
        or cast(float, initial_capital) <= 0
        or type(margin_per_pair) not in (int, float)
        or not math.isfinite(cast(float, margin_per_pair))
        or cast(float, margin_per_pair) <= 0
        or not isinstance(benchmark, Mapping)
        or not benchmark
    ):
        raise W0Error("Недостаточно данных для оценки счёта carry.")
    benchmark_days = sorted(benchmark)
    if any(
        type(benchmark[d]) not in (int, float)
        or not math.isfinite(benchmark[d])
        or benchmark[d] <= 0
        for d in benchmark_days
    ):
        raise W0Error("Некорректные отметки LQDT.")

    def mark(day):
        index = bisect_right(benchmark_days, day) - 1
        if index < 0:
            raise W0Error("Нет отметки LQDT на начало carry-окна.")
        return benchmark[benchmark_days[index]]

    start = mark(rows[0]["date"])
    return cast(float, initial_capital), cast(float, margin_per_pair), mark, start


def _apply_account(daily, quantity, account):
    initial_capital, margin_per_pair, mark, start = account
    values, _ = lqdt_overlay_values(
        initial_capital,
        [r["cumulative_gross_pnl"] - r["cumulative_transaction_costs"] for r in daily],
        [margin_per_pair * quantity if r["position"] else 0.0 for r in daily],
        [mark(r["date"]) for r in daily],
    )
    peak = 0.0
    for row, value in zip(daily, values, strict=True):
        base = initial_capital * mark(row["date"]) / start
        row["strategy_value_proxy"] = round(value, 8)
        row["benchmark_value"] = round(base, 8)
        row["X"] = round(value - row["benchmark_value"], 8)
        peak = max(peak, row["X"])
        row["H"] = round(peak, 8)
        row["D"] = round(peak - row["X"], 8)


def _expiry_cutoff(dates, expiry, expiry_buffer_days, assignment_window_trading_days):
    observed = [d for d in dates if d < expiry]
    assignment = (
        observed[-assignment_window_trading_days]
        if observed
        and observed[-1] >= expiry - timedelta(days=15)
        and len(observed) >= assignment_window_trading_days
        else None
    )
    cutoff = expiry - timedelta(days=expiry_buffer_days)
    if assignment is not None:
        cutoff = min(cutoff, assignment)
    return cutoff


def _settle_carry_exit(active, trade, row, trade_gross, funding, fee, signals, trades):
    if active is not trade:
        raise W0Error("Carry-выход без соответствующего входа.")
    trade_gross -= funding
    signals.append(
        {
            "date": row["date"],
            "action": "exit",
            "reason": trade["reason"],
            "execution_available": True,
        }
    )
    trades.append(
        {
            "entry_decision_date": trade["signal_date"],
            "entry_execution_date": trade["entry_date"],
            "exit_decision_date": row["date"],
            "exit_execution_date": row["date"],
            "status": "closed_proxy_round_trip",
            "gross_pnl": round(trade_gross, 8),
            "transaction_costs": 2 * fee,
            "net_pnl": round(trade_gross - 2 * fee, 8),
        }
    )
    return trade_gross


def _begin_carry_entry(active, trade, signals):
    if active is not None:
        raise W0Error("Перекрывающиеся позиции carry.")
    signals.append(
        {
            "date": trade["signal_date"],
            "action": "entry",
            "reason": trade.get("entry_reason"),
            "execution_available": True,
        }
    )


def _append_open_trade(active, trades, trade_gross, fee):
    if active is None:
        return
    if not active["open_at_end"]:
        raise W0Error("Незакрытая позиция carry не помечена как конечная.")
    trades.append(
        {
            "entry_decision_date": active["signal_date"],
            "entry_execution_date": active["entry_date"],
            "exit_decision_date": None,
            "exit_execution_date": None,
            "status": "open_at_data_end_no_executable_exit",
            "gross_pnl": round(trade_gross, 8),
            "transaction_costs": fee,
            "net_pnl": round(trade_gross - fee, 8),
        }
    )


def replay_carry_intents(
    study,
    intents,
    *,
    quantity,
    side_cost,
    far_expiry,
    expiry_buffer_days=5,
    assignment_window_trading_days=10,
    initial_capital=None,
    margin_per_pair=None,
    benchmark=None,
):
    """Replay short-near carry on EOD settlements, rejecting unsafe horizons.

    Ordinary exit receives the price move but not the exit-day funding; an open
    end-of-sample position includes its final clearing and pays no exit fee.
    Expiry and the native assignment window apply even if a plugin ignores them.
    The inputs must already have passed the intent boundary and funding evidence
    resolution; this routine does not establish signal causality.
    """
    _validate_inputs(
        study, intents, quantity, side_cost, expiry_buffer_days, assignment_window_trading_days
    )
    rows, expiry, dates = _validated_dates(study, far_expiry)
    cutoff = _expiry_cutoff(dates, expiry, expiry_buffer_days, assignment_window_trading_days)
    entries, exits = _indexed_intents(rows, dates, expiry, cutoff, intents)
    active = None
    trade_gross = 0.0
    daily, trades, signals = [], [], []
    fee = float(side_cost) * quantity
    if not math.isfinite(fee):
        raise W0Error("Переполнение издержек carry.")
    cumulative_gross = cumulative_cost = 0.0
    full = initial_capital is not None or margin_per_pair is not None or benchmark is not None
    account = _account_config(initial_capital, margin_per_pair, benchmark, rows) if full else None
    for i, row in enumerate(rows):
        increment = funding = 0.0
        if active is not None:
            increment = quantity * float(row["daily_gross_carry_pnl"])
            funding = quantity * float(row["daily_funding_received_short_near"])
            trade_gross += increment
        if i in exits:
            trade = exits[i]
            trade_gross = _settle_carry_exit(
                active, trade, row, trade_gross, funding, fee, signals, trades
            )
            increment -= funding
            funding = 0.0
            cumulative_cost += fee
            active = None
        if i in entries:
            trade = entries[i]
            _begin_carry_entry(active, trade, signals)
            active = trade
            trade_gross = 0.0
            cumulative_cost += fee
        cumulative_gross += increment
        if not all(
            map(math.isfinite, (increment, funding, cumulative_gross, cumulative_cost, trade_gross))
        ):
            raise W0Error("Переполнение денежного результата carry.")
        daily.append(
            {
                "date": row["date"],
                "position": int(active is not None),
                "gross_pnl_increment": round(increment, 8),
                "near_settle": row["near_settle"],
                "far_settle": row["far_settle"],
                "near_volume": row.get("near_volume"),
                "far_volume": row.get("far_volume"),
                "daily_funding_included_rub": round(funding, 8),
                "cumulative_gross_pnl": round(cumulative_gross, 8),
                "cumulative_transaction_costs": round(cumulative_cost, 8),
            }
        )
    if account is not None:
        _apply_account(daily, quantity, account)
    _append_open_trade(active, trades, trade_gross, fee)
    return {"daily": daily, "trades": trades, "signals": signals}
