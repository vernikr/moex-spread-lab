"""Settlement carry accounting for already validated position decisions.

Only the trusted carry study supplies prices and cash flows. A decision cannot
provide a price, funding rate, fee or P&L. Composed into the carry adapter.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from typing import Any, cast

from moex_spread_lab.accounting.costs import lqdt_overlay_values
from moex_spread_lab.core.w0 import W0Error

type Row = Mapping[str, Any]
type Trade = Mapping[str, Any]
type Acct = tuple[float, float, Callable[[str], float], float]
type AccountTerms = tuple[float, float, Mapping[str, float]]


def _validate_inputs(
    study: object,
    intents: object,
    quantity: int,
    side_cost: float,
    buffer_days: int,
    window_days: int,
) -> None:
    if not isinstance(study, Mapping):
        raise W0Error("Некорректные параметры учёта carry.")
    raw = cast(Mapping[str, Any], study).get("rows")
    if (
        not isinstance(raw, list)
        or not raw
        or not isinstance(intents, list)
        or type(quantity) is not int
        or quantity < 1
        or type(side_cost) not in (int, float)
        or not math.isfinite(side_cost)
        or side_cost < 0
        or type(buffer_days) is not int
        or buffer_days < 0
        or type(window_days) is not int
        or window_days < 1
    ):
        raise W0Error("Некорректные параметры учёта carry.")


def _validate_rows(rows: list[Row]) -> None:
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


def _validated_dates(
    study: Mapping[str, Any], far_expiry: str | None
) -> tuple[list[Row], date, list[date]]:
    rows: list[Row] = study["rows"]
    try:
        expiry = date.fromisoformat(cast(str, far_expiry))
        dates = [date.fromisoformat(r["date"]) for r in rows]
        if dates != sorted(set(dates)):
            raise ValueError
        _validate_rows(rows)
    except (TypeError, KeyError, ValueError, OverflowError):
        raise W0Error("Некорректные даты или денежные данные carry.") from None
    return rows, expiry, dates


def _invalid_intent(
    trade: Trade,
    positions: tuple[int, int, int, int, bool, object],
    horizon: tuple[list[date], date, date],
) -> bool:
    e, x, sig, prev, terminal, reason = positions
    dates, exp, cutoff = horizon
    return (
        trade.get("side") != "short_spread"
        or type(terminal) is not bool
        or not isinstance(reason, str)
        or not reason
        or sig > e
        or e >= x
        or e <= prev
        or dates[e] >= cutoff
        or dates[x] >= exp
        or _invalid_exit(dates, e, x, cutoff, terminal)
    )


def _invalid_exit(dates: list[date], entry: int, exit_: int, cutoff: date, terminal: bool) -> bool:
    if terminal:
        return exit_ != len(dates) - 1 or dates[exit_] >= cutoff
    return dates[exit_] > cutoff and any(day >= cutoff for day in dates[entry:exit_])


def _indexed_intents(
    rows: list[Row], dates: list[date], expiry: date, cutoff: date, intents: list[Any]
) -> tuple[dict[int, Trade], dict[int, Trade]]:
    by_day = {r["date"]: i for i, r in enumerate(rows)}
    entries: dict[int, Trade] = {}
    exits: dict[int, Trade] = {}
    prev = -1
    for item in intents:
        if not isinstance(item, Mapping):
            raise W0Error("Некорректная carry-сделка.")
        trade = cast(Trade, item)
        try:
            e, x, sig = (by_day[trade[k]] for k in ("entry_date", "exit_date", "signal_date"))
            terminal = trade["open_at_end"]
            reason = trade["reason"]
        except (KeyError, TypeError):
            raise W0Error("Неизвестная дата или поле carry-сделки.") from None
        if _invalid_intent(trade, (e, x, sig, prev, terminal, reason), (dates, expiry, cutoff)):
            raise W0Error("Carry-сделка нарушает порядок, экспирацию или окно поставки.")
        entries[e] = trade
        if not terminal:
            exits[x] = trade
        prev = x
    return entries, exits


def _account_config(
    initial_capital: object, margin_per_pair: object, benchmark: object, rows: list[Row]
) -> Acct:
    capital = cast(float, initial_capital)
    margin = cast(float, margin_per_pair)
    if (
        type(initial_capital) not in (int, float)
        or not math.isfinite(capital)
        or capital <= 0
        or type(margin_per_pair) not in (int, float)
        or not math.isfinite(margin)
        or margin <= 0
        or not isinstance(benchmark, Mapping)
        or not benchmark
    ):
        raise W0Error("Недостаточно данных для оценки счёта carry.")
    marks = cast(Mapping[str, float], benchmark)
    days = sorted(marks)
    if any(
        type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        for value in marks.values()
    ):
        raise W0Error("Некорректные отметки LQDT.")

    def mark(day: str) -> float:
        index = bisect_right(days, day) - 1
        if index < 0:
            raise W0Error("Нет отметки LQDT на начало carry-окна.")
        return marks[days[index]]

    start = mark(rows[0]["date"])
    return capital, margin, mark, start


def _apply_account(daily: list[dict[str, Any]], quantity: int, account: Acct) -> None:
    capital, margin, mark, start = account
    overlay = lqdt_overlay_values(
        capital,
        [r["cumulative_gross_pnl"] - r["cumulative_transaction_costs"] for r in daily],
        [margin * quantity if r["position"] else 0.0 for r in daily],
        [mark(r["date"]) for r in daily],
    )
    peak = 0.0
    for row, value in zip(daily, overlay.values, strict=True):
        row["strategy_value_proxy"] = round(value, 8)
        row["benchmark_value"] = round(capital * mark(row["date"]) / start, 8)
        row["X"] = round(value - row["benchmark_value"], 8)
        peak = max(peak, row["X"])
        row["H"] = round(peak, 8)
        row["D"] = round(peak - row["X"], 8)


def _expiry_cutoff(dates: list[date], expiry: date, buffer_days: int, window_days: int) -> date:
    observed = [d for d in dates if d < expiry]
    assignment = (
        observed[-window_days]
        if observed and observed[-1] >= expiry - timedelta(days=15) and len(observed) >= window_days
        else None
    )
    cutoff = expiry - timedelta(days=buffer_days)
    if assignment is not None:
        cutoff = min(cutoff, assignment)
    return cutoff


def _settle_carry_exit(
    active: Trade | None,
    trade: Trade,
    row: Row,
    gross: float,
    funding: float,
    fee: float,
    output: tuple[list[dict[str, Any]], list[dict[str, Any]]],
) -> float:
    signals, trades = output
    if active is not trade:
        raise W0Error("Carry-выход без соответствующего входа.")
    gross -= funding
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
            "gross_pnl": round(gross, 8),
            "transaction_costs": 2 * fee,
            "net_pnl": round(gross - 2 * fee, 8),
        }
    )
    return gross


def _begin_carry_entry(active: Trade | None, trade: Trade, signals: list[dict[str, Any]]) -> None:
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


def _append_open_trade(
    active: Trade | None, trades: list[dict[str, Any]], gross: float, fee: float
) -> None:
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
            "gross_pnl": round(gross, 8),
            "transaction_costs": fee,
            "net_pnl": round(gross - fee, 8),
        }
    )


def replay_carry_intents(
    study: Mapping[str, Any],
    intents: list[Any],
    *,
    quantity: int,
    side_cost: float,
    far_expiry: str | None,
    window: tuple[int, int] = (5, 10),
    account: AccountTerms | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Replay short-near carry on EOD settlements, rejecting unsafe horizons.

    An ordinary exit receives the price move but not exit-day funding; an open
    end-of-sample position includes its final clearing and pays no exit fee.
    Expiry and the native assignment window always apply. ``window`` is
    (expiry_buffer_days, assignment_window_trading_days); ``account`` is
    (initial_capital, margin_per_pair, benchmark) and switches on the full ledger.
    """
    expiry_buffer_days, assignment_window_trading_days = window
    _validate_inputs(
        study, intents, quantity, side_cost, expiry_buffer_days, assignment_window_trading_days
    )
    rows, expiry, dates = _validated_dates(study, far_expiry)
    cutoff = _expiry_cutoff(dates, expiry, expiry_buffer_days, assignment_window_trading_days)
    entries, exits = _indexed_intents(rows, dates, expiry, cutoff, intents)
    active: Trade | None = None
    trade_gross = 0.0
    daily: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    fee = float(side_cost) * quantity
    if not math.isfinite(fee):
        raise W0Error("Переполнение издержек carry.")
    cumulative_gross = cumulative_cost = 0.0
    acct: Acct | None = _account_config(*account, rows) if account is not None else None
    for i, row in enumerate(rows):
        increment = funding = 0.0
        if active is not None:
            increment = quantity * float(row["daily_gross_carry_pnl"])
            funding = quantity * float(row["daily_funding_received_short_near"])
            trade_gross += increment
        if i in exits:
            trade = exits[i]
            trade_gross = _settle_carry_exit(
                active, trade, row, trade_gross, funding, fee, (signals, trades)
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
    if acct is not None:
        _apply_account(daily, quantity, acct)
    _append_open_trade(active, trades, trade_gross, fee)
    return {"daily": daily, "trades": trades, "signals": signals}
