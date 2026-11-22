"""Fail-closed boundary between calendar plugin intents and proof accounting.

A plugin supplies decisions, not prices, costs, margin or benchmark marks. The
adapter rebinds every event to the prepared, causally eligible market rows.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from moex_spread_lab.core.contracts import StrategyIntentRun, WindowSnapshot
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from ..base import PRICE_FILLS, calendar_price


def calendar_intent_trades(
    plugin, window: WindowSnapshot, params: dict[str, Any], fill: str
) -> list[dict[str, Any]]:
    """Validate unit intents and return existing calendar-accounting trade records.

    Signals can occur no later than the required preceding bar (same-bar for
    the explicitly labelled reference fill). Rows must already be the exact
    price/volume-filtered training prefix. Only a terminal open position may
    use an ineligible exit, and it remains marked rather than executable.
    """
    if (
        window.universe != CALENDAR_UNIVERSE
        or fill not in PRICE_FILLS
        or fill not in plugin.supported_fills
    ):
        raise W0Error("Неподдерживаемая вселенная или исполнение плагина.")
    rows = list(window.rows)
    dates = [r.get("date") for r in rows]
    if (
        not rows
        or any(not isinstance(d, str) or not d for d in dates)
        or dates != sorted(set(dates))
    ):
        raise W0Error("Окно плагина должно содержать уникальные упорядоченные даты.")
    lookup = {day: i for i, day in enumerate(dates)}
    result = plugin.evaluate_intents(deepcopy(window), deepcopy(params), fill)
    if (
        not isinstance(result, StrategyIntentRun)
        or result.strategy_id != plugin.strategy_id
        or result.fill != fill
        or result.parameters != params
    ):
        raise W0Error("Плагин вернул несогласованный набор сигналов.")
    if not isinstance(result.intents, tuple):
        raise W0Error("Плагин должен вернуть неизменяемый список сигналов.")
    delay, quote = PRICE_FILLS[fill]
    trades = []
    previous_exit = -1
    entry_days, exit_days = set(), set()
    for item in result.intents:
        if not isinstance(item, Mapping):
            raise W0Error("Некорректный сигнал плагина.")
        entry_day, exit_day = item.get("entry_date"), item.get("exit_date")
        signal = item.get("signal")
        try:
            ei, xi = lookup[entry_day], lookup[exit_day]
            si = lookup[signal["date"]] if isinstance(signal, Mapping) else -1
        except (KeyError, TypeError):
            raise W0Error("Даты сигнала или сделки отсутствуют в окне.") from None
        side, reason = item.get("side"), item.get("reason")
        if (
            side not in ("long_spread", "short_spread")
            or not isinstance(reason, str)
            or not reason
            or reason in {"end_of_sample", "open_at_data_end_no_executable_exit"}
        ):
            raise W0Error("Некорректное направление или причина выхода.")
        if (
            si < 0
            or si > ei - delay
            or xi <= ei
            or ei < previous_exit
            or entry_day in entry_days
            or exit_day in exit_days
        ):
            raise W0Error("Нарушен порядок событий или причинность сигналов.")
        if (
            not isinstance(signal, Mapping)
            or not isinstance(signal.get("kind"), str)
            or not signal["kind"]
        ):
            raise W0Error("Сигнал плагина должен содержать тип и дату.")
        value = signal.get("value")
        try:
            valid_value = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            valid_value = False
        if not valid_value:
            raise W0Error("Значение сигнала должно быть конечным числом.")
        forced = reason == "end_of_window"
        if forced and xi != len(rows) - 1:
            raise W0Error("Некорректная конечная отметка позиции.")
        if not rows[si].get("signal_eligible", True) or not rows[ei].get("entry_eligible", True):
            raise W0Error("Сигнал или вход на недопустимом баре.")
        if not forced and not rows[xi].get("exit_eligible", True):
            raise W0Error("Выход на недопустимом баре.")
        try:
            expected_entry, expected_exit = (
                calendar_price(rows[ei], quote),
                calendar_price(rows[xi], quote),
            )
            prices = (item.get("entry_price"), item.get("exit_price"))
            if any(
                type(p) not in (int, float) or not math.isfinite(p)
                for p in (*prices, expected_entry, expected_exit)
            ):
                raise ValueError
            if prices != (expected_entry, expected_exit):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise W0Error("Цены исполнения не совпадают с наблюдаемыми ценовыми прокси.") from None
        trades.append(
            {
                "entry": rows[ei],
                "exit": rows[xi],
                "direction": 1 if side == "long_spread" else -1,
                "entry_price": expected_entry,
                "exit_price": expected_exit,
                "reason": reason,
                "signal": dict(signal),
            }
        )
        previous_exit = xi
        entry_days.add(entry_day)
        exit_days.add(exit_day)
    return trades
