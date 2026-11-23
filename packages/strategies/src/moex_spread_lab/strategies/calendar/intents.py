"""Fail-closed boundary between calendar plugin intents and proof accounting.

A plugin supplies decisions, not prices, costs, margin or benchmark marks. The
adapter rebinds every event to the prepared, causally eligible market rows.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, cast

from moex_spread_lab.core.contracts import StrategyIntentRun, StrategyPlugin, WindowSnapshot
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from ..base import PRICE_FILLS, calendar_price


def _require_finite_num(value: Any, msg: str) -> float:
    try:
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise ValueError
        return float(value)
    except (OverflowError, ValueError):
        raise W0Error(msg) from None


def _lookup_intent_indices(
    item: Mapping[str, Any], lookup: dict[str, int]
) -> tuple[str, str, int, int, int, Mapping[str, Any]]:
    entry_day = item.get("entry_date")
    exit_day = item.get("exit_date")
    signal = item.get("signal")
    if not isinstance(entry_day, str) or not isinstance(exit_day, str):
        raise W0Error("Даты сигнала или сделки отсутствуют в окне.")
    if not isinstance(signal, Mapping):
        raise W0Error("Сигнал плагина должен содержать тип и дату.")
    signal = cast(Mapping[str, Any], signal)
    if (
        not isinstance(signal.get("kind"), str)
        or not signal["kind"]
        or not isinstance(signal.get("date"), str)
    ):
        raise W0Error("Сигнал плагина должен содержать тип и дату.")
    try:
        ei, xi, si = lookup[entry_day], lookup[exit_day], lookup[signal["date"]]
    except KeyError:
        raise W0Error("Даты сигнала или сделки отсутствуют в окне.") from None
    _require_finite_num(signal.get("value"), "Значение сигнала должно быть конечным числом.")
    return entry_day, exit_day, ei, xi, si, signal


def _verify_calendar_prices(
    item: Mapping[str, Any], entry_row: dict[str, Any], exit_row: dict[str, Any], quote: str
) -> tuple[float, float]:
    try:
        expected_entry = calendar_price(entry_row, quote)
        expected_exit = calendar_price(exit_row, quote)
        p_ent = _require_finite_num(item.get("entry_price"), "")
        p_ext = _require_finite_num(item.get("exit_price"), "")
        if not (math.isfinite(expected_entry) and math.isfinite(expected_exit)):
            raise ValueError
        if (p_ent, p_ext) != (expected_entry, expected_exit):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError, W0Error):
        raise W0Error("Цены исполнения не совпадают с наблюдаемыми ценовыми прокси.") from None
    return expected_entry, expected_exit


@dataclass
class _IntentScanState:
    rows: list[dict[str, Any]]
    lookup: dict[str, int]
    delay: int
    quote: str
    previous_exit: int = -1
    entry_days: set[str] = field(default_factory=set)
    exit_days: set[str] = field(default_factory=set)


def _validate_calendar_intent(item: Mapping[str, Any], st: _IntentScanState) -> dict[str, Any]:
    entry_day, exit_day, ei, xi, si, signal = _lookup_intent_indices(item, st.lookup)
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
        or si > ei - st.delay
        or xi <= ei
        or ei < st.previous_exit
        or entry_day in st.entry_days
        or exit_day in st.exit_days
    ):
        raise W0Error("Нарушен порядок событий или причинность сигналов.")
    forced = reason == "end_of_window"
    if forced and xi != len(st.rows) - 1:
        raise W0Error("Некорректная конечная отметка позиции.")
    if not st.rows[si].get("signal_eligible", True) or not st.rows[ei].get("entry_eligible", True):
        raise W0Error("Сигнал или вход на недопустимом баре.")
    if not forced and not st.rows[xi].get("exit_eligible", True):
        raise W0Error("Выход на недопустимом баре.")
    expected_entry, expected_exit = _verify_calendar_prices(
        item, st.rows[ei], st.rows[xi], st.quote
    )
    st.entry_days.add(entry_day)
    st.exit_days.add(exit_day)
    st.previous_exit = xi
    return {
        "entry": st.rows[ei],
        "exit": st.rows[xi],
        "direction": 1 if side == "long_spread" else -1,
        "entry_price": expected_entry,
        "exit_price": expected_exit,
        "reason": reason,
        "signal": dict(signal),
    }


def calendar_intent_trades(
    plugin: StrategyPlugin, window: WindowSnapshot, params: dict[str, Any], fill: str
) -> list[dict[str, Any]]:
    """Run ``plugin`` on defensive copies, then re-price and verify every trade."""
    if (
        window.universe != CALENDAR_UNIVERSE
        or fill not in PRICE_FILLS
        or fill not in plugin.supported_fills
    ):
        raise W0Error("Неподдерживаемая вселенная или исполнение плагина.")
    rows = list(window.rows)
    dates = [r.get("date") for r in rows]
    if not rows or any(not isinstance(d, str) or not d for d in dates):
        raise W0Error("Окно плагина должно содержать уникальные упорядоченные даты.")
    dates = cast(list[str], dates)
    if dates != sorted(set(dates)):
        raise W0Error("Окно плагина должно содержать уникальные упорядоченные даты.")
    lookup = {str(day): i for i, day in enumerate(dates)}
    result: Any = plugin.evaluate_intents(deepcopy(window), deepcopy(params), fill)
    if (
        not isinstance(result, StrategyIntentRun)
        or result.strategy_id != plugin.strategy_id
        or result.fill != fill
        or result.parameters != params
    ):
        raise W0Error("Плагин вернул несогласованный набор сигналов.")
    intents: Any = result.intents
    if not isinstance(intents, tuple):
        raise W0Error("Плагин должен вернуть неизменяемый список сигналов.")
    delay, quote = PRICE_FILLS[fill]
    st = _IntentScanState(rows=rows, lookup=lookup, delay=delay, quote=quote)
    trades: list[dict[str, Any]] = []
    for item in cast(tuple[Any, ...], intents):
        if not isinstance(item, Mapping):
            raise W0Error("Некорректный сигнал плагина.")
        trades.append(_validate_calendar_intent(cast(Mapping[str, Any], item), st))
    return trades
