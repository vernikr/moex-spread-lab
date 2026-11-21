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
    dates: Sequence[str], trades: Sequence[Mapping], rates: Mapping[str, float | None],
    *, near_lot_size: float, quantity: int,
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

    if (not dates or any(not isinstance(day, str) or not day for day in dates)
            or list(dates) != sorted(set(dates)) or not isinstance(rates, Mapping)
            or not finite(near_lot_size) or near_lot_size <= 0
            or type(quantity) is not int or quantity <= 0):
        raise W0Error("Некорректные входные данные для расчёта фондирования.")
    index = {day: i for i, day in enumerate(dates)}
    if any(day not in index or (rate is not None and not finite(rate)) for day, rate in rates.items()):
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
        if (side not in ("long_spread", "short_spread") or type(terminal) is not bool
                or entry >= exit_ or entry <= last_exit or (terminal and exit_ != len(dates) - 1)):
            raise W0Error("Нарушен порядок позиций для расчёта фондирования.")
        position = 1 if side == "long_spread" else -1
        for i in range(entry, exit_ + int(terminal)):
            positions[i] = position
        last_exit = exit_
    result = []
    for day, position in zip(dates, positions, strict=True):
        rate = rates.get(day)
        try:
            value = None if position and rate is None else -position * rate * near_lot_size * quantity if position else 0.0
        except OverflowError:
            raise W0Error("Переполнение расчёта фондирования.") from None
        if value is not None and not finite(value):
            raise W0Error("Переполнение расчёта фондирования.")
        result.append({"date": day, "position_at_clearing": position, "funding_rub": value})
    return result
