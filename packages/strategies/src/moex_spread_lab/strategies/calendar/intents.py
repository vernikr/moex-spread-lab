"""Fail-closed boundary between calendar plugin intents and proof accounting.

A plugin supplies decisions, not prices, costs, margin or benchmark marks. The
adapter rebinds every event to the prepared, causally eligible market rows.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from moex_spread_lab.core.contracts import StrategyPlugin, WindowSnapshot
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from ..base import (
    PRICE_FILLS,
    IntentSpanTracker,
    build_date_index,
    calendar_price,
    evaluate_plugin_intents,
    require_finite_number,
    validate_signal_payload,
)

_FORBIDDEN_REASONS = frozenset({"end_of_sample", "open_at_data_end_no_executable_exit"})


def _verify_calendar_prices(
    item: Mapping[str, Any], entry_row: dict[str, Any], exit_row: dict[str, Any], quote: str
) -> tuple[float, float]:
    try:
        expected_entry = calendar_price(entry_row, quote)
        expected_exit = calendar_price(exit_row, quote)
        p_ent = require_finite_number(item.get("entry_price"), "")
        p_ext = require_finite_number(item.get("exit_price"), "")
        if not (math.isfinite(expected_entry) and math.isfinite(expected_exit)):
            raise ValueError
        if (p_ent, p_ext) != (expected_entry, expected_exit):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError, W0Error):
        raise W0Error("Цены исполнения не совпадают с наблюдаемыми ценовыми прокси.") from None
    return expected_entry, expected_exit


def _validate_calendar_intent(
    item: Mapping[str, Any], lookup: dict[str, int], quote: str, tracker: IntentSpanTracker
) -> dict[str, Any]:
    side, reason, signal = validate_signal_payload(item, forbidden_reasons=_FORBIDDEN_REASONS)
    entry_day, exit_day, sig_day = item.get("entry_date"), item.get("exit_date"), signal.get("date")
    if (
        not isinstance(entry_day, str)
        or not isinstance(exit_day, str)
        or not isinstance(sig_day, str)
    ):
        raise W0Error("Даты сигнала или сделки отсутствуют в окне.")
    try:
        ei, xi, si = lookup[entry_day], lookup[exit_day], lookup[sig_day]
    except KeyError:
        raise W0Error("Даты сигнала или сделки отсутствуют в окне.") from None
    terminal_open = bool(item.get("open_at_end"))
    forced = reason == "end_of_window" or terminal_open
    tracker.check_and_advance(
        (si, ei, xi), (entry_day, exit_day), forced=forced, terminal_open=terminal_open
    )
    expected_entry, expected_exit = _verify_calendar_prices(
        item, tracker.rows[ei], tracker.rows[xi], quote
    )
    return {
        "entry": tracker.rows[ei],
        "exit": tracker.rows[xi],
        "direction": 1 if side == "long_spread" else -1,
        "entry_price": expected_entry,
        "exit_price": expected_exit,
        "reason": reason,
        "signal": dict(signal),
    }


def _prepare_verifier_rows(window: WindowSnapshot, quote: str) -> list[dict[str, Any]]:
    if window.timeframe == "1h":
        return [dict(r) for r in window.rows]
    out: list[dict[str, Any]] = []
    for src in window.rows:
        row = dict(src)
        if "min_volume" in row and (row.get("min_volume") or 0) <= 0:
            row["signal_eligible"] = False
            row["entry_eligible"] = False
            row["exit_eligible"] = False
        elif quote == "open" and row.get("has_open_quote") is False:
            row["entry_eligible"] = False
            row["exit_eligible"] = False
        out.append(row)
    return out


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
    delay, quote = PRICE_FILLS[fill]
    rows = _prepare_verifier_rows(window, quote)
    _, lookup = build_date_index(
        rows, "Окно плагина должно содержать уникальные упорядоченные даты."
    )
    intents = evaluate_plugin_intents(
        plugin,
        window,
        params,
        fill,
        tuple_msg="Плагин должен вернуть неизменяемый список сигналов.",
    )
    tracker = IntentSpanTracker(rows=rows, delay=delay)
    return [_validate_calendar_intent(item, lookup, quote, tracker) for item in intents]
