"""Validate future pair-price plugin intents before a proof accounting adapter."""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, cast

from moex_spread_lab.core.contracts import StrategyIntentRun, StrategyPlugin, WindowSnapshot
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from ..base import PRICE_FILLS


def _parse_1d_date(value: str) -> str | None:
    if len(value) > 10 and value[10:].replace("T", " ") != " 00:00:00":
        return None
    day = value[:10]
    return day if date.fromisoformat(day).isoformat() == day else None


def _parse_1h_stamp(value: str) -> str | None:
    if len(value) != 19:
        return None
    stamp = value.replace(" ", "T")
    return stamp if datetime.fromisoformat(stamp).isoformat(timespec="seconds") == stamp else None


def _date(value: Any, timeframe: str) -> str:
    if not isinstance(value, str) or len(value) < 10:
        raise W0Error("Некорректная дата сигнала или сделки.")
    try:
        parsed = (
            _parse_1d_date(value)
            if timeframe == "1d"
            else _parse_1h_stamp(value)
            if timeframe == "1h"
            else None
        )
    except ValueError:
        parsed = None
    if parsed is None:
        raise W0Error("Неподдерживаемый формат времени пары.")
    return parsed


def _number(value: Any) -> float:
    try:
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise ValueError
        return float(value)
    except (OverflowError, ValueError):
        raise W0Error("Требуется конечное числовое значение.") from None


def _extract_leg_multipliers(window: WindowSnapshot) -> list[float]:
    try:
        contracts = [window.contracts[secid] for secid in (window.near, window.far)]
        sizes = [(_number(c["tick_value"]), _number(c["tick_size"])) for c in contracts]
        if any(value <= 0 or step <= 0 for value, step in sizes):
            raise ValueError
        multipliers = [value / step for value, step in sizes]
    except (KeyError, TypeError, ZeroDivisionError, ValueError, W0Error):
        raise W0Error("Нет параметров стоимости пункта обеих ног.") from None
    if any(mult <= 0 or not math.isfinite(mult) for mult in multipliers):
        raise W0Error("Некорректная стоимость пункта ноги.")
    return multipliers


@dataclass
class _PairIntentState:
    rows: list[dict[str, Any]]
    index: dict[str, int]
    timeframe: str
    delay: int
    quote: str
    multipliers: list[float]
    previous_exit: int = -1
    entries: set[str] = field(default_factory=set)
    exits: set[str] = field(default_factory=set)


def _verify_event_fills(
    item: Mapping[str, Any],
    event_rows: tuple[dict[str, Any], dict[str, Any]],
    event_days: tuple[str, str],
    forced: bool,
    st: _PairIntentState,
) -> tuple[dict[str, Any], dict[str, Any]]:
    entry_row, exit_row = event_rows
    entry_day, exit_day = event_days
    event_fills: list[dict[str, Any]] = []
    for event, row, price_kind, day in (
        ("entry", entry_row, st.quote, entry_day),
        ("exit", exit_row, "close" if forced else st.quote, exit_day),
    ):
        prices: list[float] = []
        for leg in ("near", "far"):
            expected = _number(row.get(f"{leg}_{price_kind}"))
            proposed = _number(item.get(f"{event}_{leg}_fill"))
            if expected <= 0 or proposed != expected:
                raise W0Error("Цены исполнения ног не совпадают с ценовыми прокси.")
            prices.append(expected)
        event_fills.append(
            {
                "date": day,
                "near_price": prices[0],
                "far_price": prices[1],
                "spread_fill": round(
                    prices[0] * st.multipliers[0] - prices[1] * st.multipliers[1], 8
                ),
            }
        )
    return event_fills[0], event_fills[1]


def _validate_pair_intent(item: object, st: _PairIntentState) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise W0Error("Некорректный сигнал пары.")
    intent = cast(Mapping[str, Any], item)
    if not isinstance(intent.get("signal"), Mapping):
        raise W0Error("Некорректный сигнал пары.")
    signal = cast(Mapping[str, Any], intent["signal"])
    try:
        entry_day = _date(intent.get("entry_date"), st.timeframe)
        exit_day = _date(intent.get("exit_date"), st.timeframe)
        signal_day = _date(signal.get("date"), st.timeframe)
        ei, xi, si = st.index[entry_day], st.index[exit_day], st.index[signal_day]
    except KeyError:
        raise W0Error("Даты сигнала или сделки отсутствуют в окне.") from None
    side, reason = intent.get("side"), intent.get("reason")
    if (
        side not in ("long_spread", "short_spread")
        or not isinstance(reason, str)
        or not reason
        or reason in {"end_of_window", "open_at_data_end_no_executable_exit"}
    ):
        raise W0Error("Некорректное направление или причина выхода.")
    if (
        si > ei - st.delay
        or xi <= ei
        or ei < st.previous_exit
        or entry_day in st.entries
        or exit_day in st.exits
    ):
        raise W0Error("Нарушен порядок событий или причинность сигналов.")
    if not isinstance(signal.get("kind"), str) or not signal["kind"]:
        raise W0Error("Сигнал должен содержать тип.")
    _number(signal.get("value"))
    forced = reason == "end_of_sample"
    if forced and xi != len(st.rows) - 1:
        raise W0Error("Некорректная конечная отметка позиции.")
    if not st.rows[si].get("signal_eligible", True) or not st.rows[ei].get("entry_eligible", True):
        raise W0Error("Сигнал или вход на недопустимом баре.")
    if not forced and not st.rows[xi].get("exit_eligible", True):
        raise W0Error("Выход на недопустимом баре.")
    ent_fill, ext_fill = _verify_event_fills(
        intent, (st.rows[ei], st.rows[xi]), (entry_day, exit_day), forced, st
    )
    st.entries.add(entry_day)
    st.exits.add(exit_day)
    st.previous_exit = xi
    return {
        "side": side,
        "signal": {**signal, "date": signal_day},
        "entry": ent_fill,
        "exit": ext_fill,
        "exit_reason": reason,
        "open_at_end": forced,
    }


def pair_price_intent_trades(
    plugin: StrategyPlugin,
    window: WindowSnapshot,
    params: dict[str, Any],
    fill: str,
    *,
    isolate: bool = True,
) -> list[dict[str, Any]]:
    """Rebind unit pair decisions to actual two-leg price proxies; fail closed.

    `isolate=False` is reserved for first-party built-ins, which never mutate their
    window; drop-in plugins always receive private copies.
    """
    if (
        window.universe != PAIR_UNIVERSE
        or fill not in PRICE_FILLS
        or fill not in plugin.supported_fills
    ):
        raise W0Error("Неподдерживаемая вселенная или исполнение плагина.")
    rows = list(window.rows)
    dates = [_date(r.get("date"), window.timeframe) for r in rows]
    if not rows or dates != sorted(set(dates)):
        raise W0Error("Окно пары должно содержать уникальные упорядоченные даты.")
    index = {day: i for i, day in enumerate(dates)}
    multipliers = _extract_leg_multipliers(window)
    for row in rows:
        if any(_number(row.get(f"{leg}_volume")) <= 0 for leg in ("near", "far")):
            raise W0Error("Строка пары не прошла проверку объёмов.")
    result: Any = (
        plugin.evaluate_intents(deepcopy(window), deepcopy(params), fill)
        if isolate
        else plugin.evaluate_intents(window, dict(params), fill)
    )
    if (
        not isinstance(result, StrategyIntentRun)
        or result.strategy_id != plugin.strategy_id
        or result.fill != fill
        or result.parameters != params
    ):
        raise W0Error("Плагин вернул несогласованный набор сигналов.")
    intents: Any = result.intents
    if not isinstance(intents, tuple):
        raise W0Error("Плагин вернул несогласованный набор сигналов.")
    intent_items = cast(tuple[object, ...], intents)
    delay, quote = PRICE_FILLS[fill]
    st = _PairIntentState(
        rows=rows,
        index=index,
        timeframe=window.timeframe,
        delay=delay,
        quote=quote,
        multipliers=multipliers,
    )
    return [_validate_pair_intent(item, st) for item in intent_items]
