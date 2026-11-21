"""Validate future pair-price plugin intents before a proof accounting adapter.

This boundary does not calculate P&L, costs, funding or capital. It is deliberately
not registered as a runtime proof adapter until those paths are integrated and
verified against the existing accounting and funding contracts.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime
from typing import Any

from moex_spread_lab.core.contracts import StrategyIntentRun, WindowSnapshot
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from ..base import PRICE_FILLS


def _date(value: Any, timeframe: str) -> str:
    if not isinstance(value, str) or len(value) < 10:
        raise W0Error("Некорректная дата сигнала или сделки.")
    # The legacy daily engine emits a midnight timestamp while its proof uses a date.
    try:
        if timeframe == "1d":
            if len(value) > 10 and value[10:].replace("T", " ") != " 00:00:00":
                raise ValueError
            day = value[:10]
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
            return day
        if timeframe == "1h" and len(value) == 19:
            stamp = value.replace(" ", "T")
            if datetime.fromisoformat(stamp).isoformat(timespec="seconds") != stamp:
                raise ValueError
            return stamp
    except ValueError:
        pass
    raise W0Error("Неподдерживаемый формат времени пары.")


def _number(value: Any) -> float:
    try:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError
        return float(value)
    except (OverflowError, ValueError):
        raise W0Error("Требуется конечное числовое значение.") from None


def pair_price_intent_trades(plugin, window: WindowSnapshot, params: dict[str, Any], fill: str) -> list[dict[str, Any]]:
    """Rebind unit pair decisions to actual two-leg price proxies; fail closed.

    Rows must be the positive-volume, synchronized analysis prefix. A forced
    end-of-sample mark uses the final close even for a next-open entry; it is
    explicitly marked as open, not an executable exit. The adapter never trusts
    the plugin to provide costs, funding or a profit figure.
    """
    if window.universe != PAIR_UNIVERSE or fill not in PRICE_FILLS or fill not in plugin.supported_fills:
        raise W0Error("Неподдерживаемая вселенная или исполнение плагина.")
    rows = list(window.rows)
    dates = [_date(r.get("date"), window.timeframe) for r in rows]
    if not rows or dates != sorted(set(dates)):
        raise W0Error("Окно пары должно содержать уникальные упорядоченные даты.")
    index = {day: i for i, day in enumerate(dates)}
    try:
        contracts = [window.contracts[secid] for secid in (window.near, window.far)]
        sizes = [(_number(c["tick_value"]), _number(c["tick_size"])) for c in contracts]
        if any(value <= 0 or step <= 0 for value, step in sizes):
            raise ValueError
        multipliers = [value / step for value, step in sizes]
    except (KeyError, TypeError, ZeroDivisionError, ValueError):
        raise W0Error("Нет параметров стоимости пункта обеих ног.") from None
    if any(mult <= 0 or not math.isfinite(mult) for mult in multipliers):
        raise W0Error("Некорректная стоимость пункта ноги.")
    for row in rows:
        if any(_number(row.get(f"{leg}_volume")) <= 0 for leg in ("near", "far")):
            raise W0Error("Строка пары не прошла проверку объёмов.")
    result = plugin.evaluate_intents(deepcopy(window), deepcopy(params), fill)
    if not isinstance(result, StrategyIntentRun) or result.strategy_id != plugin.strategy_id or result.fill != fill or result.parameters != params or not isinstance(result.intents, tuple):
        raise W0Error("Плагин вернул несогласованный набор сигналов.")
    delay, quote = PRICE_FILLS[fill]
    trades = []
    previous_exit = -1
    entries, exits = set(), set()
    for item in result.intents:
        if not isinstance(item, Mapping) or not isinstance(item.get("signal"), Mapping):
            raise W0Error("Некорректный сигнал пары.")
        signal = item["signal"]
        try:
            entry_day = _date(item.get("entry_date"), window.timeframe)
            exit_day = _date(item.get("exit_date"), window.timeframe)
            signal_day = _date(signal.get("date"), window.timeframe)
            ei, xi, si = index[entry_day], index[exit_day], index[signal_day]
        except KeyError:
            raise W0Error("Даты сигнала или сделки отсутствуют в окне.") from None
        side, reason = item.get("side"), item.get("reason")
        if (side not in ("long_spread", "short_spread") or not isinstance(reason, str) or not reason
                or reason in {"end_of_window", "open_at_data_end_no_executable_exit"}):
            raise W0Error("Некорректное направление или причина выхода.")
        if (si > ei - delay or xi <= ei or ei < previous_exit
                or entry_day in entries or exit_day in exits):
            raise W0Error("Нарушен порядок событий или причинность сигналов.")
        if not isinstance(signal.get("kind"), str) or not signal["kind"]:
            raise W0Error("Сигнал должен содержать тип.")
        _number(signal.get("value"))
        forced = reason == "end_of_sample"
        if forced and xi != len(rows) - 1:
            raise W0Error("Некорректная конечная отметка позиции.")
        if not rows[si].get("signal_eligible", True) or not rows[ei].get("entry_eligible", True):
            raise W0Error("Сигнал или вход на недопустимом баре.")
        if not forced and not rows[xi].get("exit_eligible", True):
            raise W0Error("Выход на недопустимом баре.")
        event_fills = []
        for event, row, price_kind in (("entry", rows[ei], quote), ("exit", rows[xi], "close" if forced else quote)):
            prices = []
            for leg in ("near", "far"):
                expected = _number(row.get(f"{leg}_{price_kind}"))
                proposed = _number(item.get(f"{event}_{leg}_fill"))
                if expected <= 0 or proposed != expected:
                    raise W0Error("Цены исполнения ног не совпадают с ценовыми прокси.")
                prices.append(expected)
            event_fills.append({"date": entry_day if event == "entry" else exit_day,
                                "near_price": prices[0], "far_price": prices[1],
                                "spread_fill": round(prices[0] * multipliers[0] - prices[1] * multipliers[1], 8)})
        trades.append({"side": side, "signal": {**signal, "date": signal_day},
                       "entry": event_fills[0], "exit": event_fills[1],
                       "exit_reason": reason, "open_at_end": forced})
        entries.add(entry_day)
        exits.add(exit_day)
        previous_exit = xi
    return trades
