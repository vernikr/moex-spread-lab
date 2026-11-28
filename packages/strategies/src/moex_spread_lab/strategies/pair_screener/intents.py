"""Validate future pair-price plugin intents before a proof accounting adapter."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from moex_spread_lab.core.contracts import StrategyPlugin, WindowSnapshot
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from ..base import (
    PRICE_FILLS,
    IntentSpanTracker,
    build_date_index,
    evaluate_plugin_intents,
    require_finite_number,
    validate_signal_payload,
)

_FORBIDDEN_REASONS = frozenset({"end_of_window", "open_at_data_end_no_executable_exit"})


def _parse_1d(value: str) -> str | None:
    if len(value) > 10 and value[10:].replace("T", " ") != " 00:00:00":
        return None
    day = value[:10]
    return day if date.fromisoformat(day).isoformat() == day else None


def _parse_1h(value: str) -> str | None:
    if len(value) != 19:
        return None
    stamp = value.replace(" ", "T")
    return stamp if datetime.fromisoformat(stamp).isoformat(timespec="seconds") == stamp else None


def _parse_date(value: Any, timeframe: str) -> str:
    if not isinstance(value, str) or len(value) < 10:
        raise W0Error("Некорректная дата сигнала или сделки.")
    parser = _parse_1d if timeframe == "1d" else _parse_1h if timeframe == "1h" else None
    try:
        parsed = parser(value) if parser is not None else None
    except ValueError:
        parsed = None
    if parsed is None:
        raise W0Error("Неподдерживаемый формат времени пары.")
    return parsed


def _extract_leg_multipliers(window: WindowSnapshot) -> list[float]:
    try:
        contracts = [window.contracts[secid] for secid in (window.near, window.far)]
        multipliers = [
            require_finite_number(c["tick_value"], positive=True)
            / require_finite_number(c["tick_size"], positive=True)
            for c in contracts
        ]
    except (KeyError, TypeError, ZeroDivisionError, ValueError, W0Error):
        raise W0Error("Нет параметров стоимости пункта обеих ног.") from None
    if any(mult <= 0 or not math.isfinite(mult) for mult in multipliers):
        raise W0Error("Некорректная стоимость пункта ноги.")
    return multipliers


def _verify_event_fills(
    item: Mapping[str, Any],
    event_rows: tuple[dict[str, Any], dict[str, Any]],
    event_days: tuple[str, str],
    forced: bool,
    quote: str,
    multipliers: list[float],
) -> tuple[dict[str, Any], dict[str, Any]]:
    entry_row, exit_row = event_rows
    entry_day, exit_day = event_days
    fills: list[dict[str, Any]] = []
    for event, row, price_kind, day in (
        ("entry", entry_row, quote, entry_day),
        ("exit", exit_row, "close" if forced else quote, exit_day),
    ):
        prices: list[float] = []
        for leg in ("near", "far"):
            expected = require_finite_number(row.get(f"{leg}_{price_kind}"))
            proposed = require_finite_number(item.get(f"{event}_{leg}_fill"))
            if expected <= 0 or proposed != expected:
                raise W0Error("Цены исполнения ног не совпадают с ценовыми прокси.")
            prices.append(expected)
        fills.append(
            {
                "date": day,
                "near_price": prices[0],
                "far_price": prices[1],
                "spread_fill": round(prices[0] * multipliers[0] - prices[1] * multipliers[1], 8),
            }
        )
    return fills[0], fills[1]


def _validate_pair_intent(
    item: Mapping[str, Any],
    index: dict[str, int],
    timeframe: str,
    quote: str,
    multipliers: list[float],
    tracker: IntentSpanTracker,
) -> dict[str, Any]:
    side, reason, signal = validate_signal_payload(item, forbidden_reasons=_FORBIDDEN_REASONS)
    try:
        entry_day = _parse_date(item.get("entry_date"), timeframe)
        exit_day = _parse_date(item.get("exit_date"), timeframe)
        signal_day = _parse_date(signal.get("date"), timeframe)
        ei, xi, si = index[entry_day], index[exit_day], index[signal_day]
    except KeyError:
        raise W0Error("Даты сигнала или сделки отсутствуют в окне.") from None
    forced = reason == "end_of_sample"
    tracker.check_and_advance((si, ei, xi), (entry_day, exit_day), forced=forced)
    ent_fill, ext_fill = _verify_event_fills(
        item,
        (tracker.rows[ei], tracker.rows[xi]),
        (entry_day, exit_day),
        forced,
        quote,
        multipliers,
    )
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
    rows = [
        {**r, "date": r["timestamp"]}
        if window.timeframe == "1h" and len(str(r.get("date", ""))) == 10 and "timestamp" in r
        else r
        for r in window.rows
    ]
    _, index = build_date_index(
        rows,
        "Окно пары должно содержать уникальные упорядоченные даты.",
        parse=lambda d: _parse_date(d, window.timeframe),
    )
    multipliers = _extract_leg_multipliers(window)
    for row in rows:
        for leg in ("near", "far"):
            require_finite_number(
                row.get(f"{leg}_volume"),
                "Строка пары не прошла проверку объёмов.",
                positive=True,
            )
    intents = evaluate_plugin_intents(plugin, window, params, fill, isolate=isolate)
    delay, quote = PRICE_FILLS[fill]
    tracker = IntentSpanTracker(rows=rows, delay=delay)
    return [
        _validate_pair_intent(item, index, window.timeframe, quote, multipliers, tracker)
        for item in intents
    ]
