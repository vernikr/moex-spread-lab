"""Internal pair-funding replay primitive; not a complete proof adapter.

Funding is applied after the bar's executable entry/exit, but before a terminal
non-executable close mark. Missing rates remain unknown, never a zero charge.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from numbers import Real

from moex_spread_lab.core.w0 import W0Error


def replay_pair_funding(
    dates: Sequence[str],
    trades: Sequence[Mapping],
    rates: Mapping[str, float | None],
    *,
    near_lot_size: float,
    quantity: int,
    no_clearing_dates: Sequence[str] = (),
) -> list[dict]:
    """Return per-bar signed near-leg funding; callers must establish evidence.

    Dates are unique ordered bar keys, and rates must use those exact keys. This
    deliberately does not infer an intraday clearing timestamp from a daily rate.
    Unknown rates produce None on held bars. It computes no P&L, fees or capital.
    """

    def finite(value):
        try:
            return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)
        except OverflowError:
            return False

    if (
        not dates
        or any(not isinstance(day, str) or not day for day in dates)
        or list(dates) != sorted(set(dates))
        or not isinstance(rates, Mapping)
        or not finite(near_lot_size)
        or near_lot_size <= 0
        or type(quantity) is not int
        or quantity <= 0
    ):
        raise W0Error("Некорректные входные данные для расчёта фондирования.")
    index = {day: i for i, day in enumerate(dates)}
    if any(
        day not in index or (rate is not None and not finite(rate)) for day, rate in rates.items()
    ) or any(day not in index or day in rates for day in no_clearing_dates):
        raise W0Error("Некорректная ставка фондирования или дата.")
    positions = [0] * len(dates)
    last_exit = -1
    for trade in trades:
        if not isinstance(trade, Mapping):
            raise W0Error("Некорректная сделка для расчёта фондирования.")
        try:
            entry, exit_ = index[trade["entry"]["date"]], index[trade["exit"]["date"]]
            side, terminal = trade["side"], trade["open_at_end"]
        except (KeyError, TypeError):
            raise W0Error("Некорректная сделка для расчёта фондирования.") from None
        if (
            side not in ("long_spread", "short_spread")
            or type(terminal) is not bool
            or entry >= exit_
            or entry <= last_exit
            or (terminal and exit_ != len(dates) - 1)
        ):
            raise W0Error("Нарушен порядок позиций для расчёта фондирования.")
        position = 1 if side == "long_spread" else -1
        for i in range(entry, exit_ + int(terminal)):
            positions[i] = position
        last_exit = exit_
    no_clearing = set(no_clearing_dates)
    result = []
    for day, position in zip(dates, positions, strict=True):
        rate = rates.get(day)
        status = (
            "no_clearing"
            if day in no_clearing
            else "not_held"
            if not position
            else "missing_rate"
            if rate is None
            else "applied"
        )
        try:
            value = (
                0.0
                if day in no_clearing or not position
                else None
                if rate is None
                else -position * rate * near_lot_size * quantity
            )
        except OverflowError:
            raise W0Error("Переполнение расчёта фондирования.") from None
        if value is not None and not finite(value):
            raise W0Error("Переполнение расчёта фондирования.")
        result.append(
            {
                "date": day,
                "position_at_clearing": position,
                "funding_rub": value,
                "funding_status": status,
            }
        )
    return result


def replay_daily_pair_funding(dates, trades, window, *, through=None, near_lot_size, quantity):
    """Resolve existing funding evidence for daily bars, then replay its payments.

    The window must be constructed from the same positive-volume analysis bars.
    The existing resolver enforces availability and the train/prefix cutoff; this
    function never invents intraday clearing times or fills unresolved gaps.
    """
    from copy import deepcopy
    from datetime import date

    from moex_spread_lab.strategies.funding_carry.proof_funding import FundingWindow

    if not isinstance(window, FundingWindow) or not dates:
        raise W0Error("Для дневного расчёта нужны окно и даты фондирования.")
    try:
        if any(
            not isinstance(day, str) or date.fromisoformat(day).isoformat() != day for day in dates
        ):
            raise ValueError
        if through is not None and (
            not isinstance(through, str) or date.fromisoformat(through).isoformat() != through
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise W0Error("Для дневного фондирования требуются даты YYYY-MM-DD.") from None
    expected = [day for day in window.days if through is None or day <= through]
    if list(dates) != expected or len(set(dates)) != len(dates):
        raise W0Error("Даты баров не совпадают с разрешённым окном фондирования.")
    evidence = window.resolve(through)
    if set(evidence.marks) != set(dates):
        raise W0Error("Неполное покрытие ставок фондирования.")
    no_clearing = []
    rates = {}
    for day, mark in evidence.marks.items():
        if mark.kind == "no_clearing" and mark.value is None:
            no_clearing.append(day)
        elif mark.kind in {"published", "rechecked", "midpoint"} and mark.value is not None:
            rates[day] = mark.value
        else:
            raise W0Error("Некорректная отметка ставки фондирования.")
    rows = replay_pair_funding(
        dates,
        trades,
        rates,
        near_lot_size=near_lot_size,
        quantity=quantity,
        no_clearing_dates=no_clearing,
    )
    for row in rows:
        row["rate_kind"] = evidence.marks[row["date"]].kind
        row["rate"] = evidence.marks[row["date"]].value
    return {"daily": rows, "provenance": deepcopy(evidence.provenance)}


def attach_daily_unit_funding(series, trades, window, *, through=None, near_lot_size):
    """Prepare isolated unit funding fields for the existing funded sizing replay.

    This consumes only the eligible daily analysis series. It intentionally rejects
    journal-only extras and is not a complete proof or an enabled plugin adapter.
    Quantity is fixed at one here; the sizing engine applies accepted quantities.
    """
    from copy import deepcopy

    if not isinstance(series, (list, tuple)) or any(not isinstance(row, Mapping) for row in series):
        raise W0Error("Некорректные строки анализа для фондирования.")
    if any(row.get("volume_eligible", True) is not True for row in series):
        raise W0Error("В расчёт фондирования допускаются только подходящие бары анализа.")
    dates = [row.get("date") for row in series]
    replay = replay_daily_pair_funding(
        dates, trades, window, through=through, near_lot_size=near_lot_size, quantity=1
    )
    rows = []
    for row, funding in zip(series, replay["daily"], strict=True):
        if funding["funding_rub"] is None:
            raise W0Error("Неполные данные фондирования нельзя передать в учёт.")
        # There is no contract count on an explicitly non-clearing session.
        held = bool(funding["position_at_clearing"])
        rows.append(
            {
                **deepcopy(row),
                "funding_rub": funding["funding_rub"],
                "funding_contracts": int(held and funding["funding_status"] == "applied"),
                "funding_rate_kind": funding["rate_kind"],
                "funding_swaprate_rub_per_unit": funding["rate"],
            }
        )
    return {"series": rows, "provenance": replay["provenance"]}
