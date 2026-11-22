"""Fail-closed validation of funding-carry strategy intents.

This validates decisions only; proof separately owns evidence and accounting.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from datetime import date
from typing import Any

from moex_spread_lab.core.contracts import StrategyIntentRun, StrategyPlugin, WindowSnapshot
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error


def _is_pos_finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value)) and float(value) > 0


def _validate_study_rows(rows: Any) -> list[str]:
    if not isinstance(rows, list) or not rows or any(not isinstance(r, Mapping) for r in rows):
        raise W0Error("Нет пригодного ряда расчётных цен carry.")
    try:
        days = [r["date"] for r in rows]
        if any(
            not isinstance(d, str) or len(d) != 10 or date.fromisoformat(d).isoformat() != d
            for d in days
        ) or days != sorted(set(days)):
            raise ValueError
        for row in rows:
            if not _is_pos_finite(row["near_settle"]) or not _is_pos_finite(row["far_settle"]):
                raise ValueError
    except (ValueError, TypeError, OverflowError, KeyError):
        raise W0Error("Некорректные расчётные цены или порядок дат carry.") from None
    return days


def _check_intent_order(
    side: Any,
    reason: Any,
    terminal: Any,
    signal: int,
    entry: int,
    exit_: int,
    meta: tuple[int, int],
) -> None:
    previous_exit, total_days = meta
    valid_fields = (
        side == "short_spread"
        and type(terminal) is bool
        and isinstance(reason, str)
        and bool(reason)
    )
    valid_order = signal <= entry < exit_ and entry > previous_exit
    valid_term = (not terminal or exit_ == total_days - 1) and (
        terminal or reason != "open_at_data_end_no_executable_exit"
    )
    if not (valid_fields and valid_order and valid_term):
        raise W0Error("Нарушен порядок или направление carry-сделки.")


def _check_intent_volumes(
    rows: list[Mapping[str, Any]], signal: int, entry: int, exit_: int, terminal: bool
) -> None:
    for i in (signal, entry) if terminal else (signal, entry, exit_):
        if not _is_pos_finite(rows[i].get("near_volume")) or not _is_pos_finite(
            rows[i].get("far_volume")
        ):
            raise W0Error("Недостаточные данные объёмов для carry-сделки.")


def _validate_carry_intent(
    item: Any,
    rows: list[Mapping[str, Any]],
    days: list[str],
    indices: dict[str, int],
    previous_exit: int,
) -> tuple[dict[str, Any], int]:
    if not isinstance(item, Mapping):
        raise W0Error("Некорректная сделка carry.")
    try:
        entry, exit_, signal = (
            indices[item[k]] for k in ("entry_date", "exit_date", "signal_date")
        )
        side, reason, terminal = item["side"], item["reason"], item["open_at_end"]
    except (KeyError, TypeError):
        raise W0Error("Даты или поля carry-сделки отсутствуют в окне.") from None
    _check_intent_order(side, reason, terminal, signal, entry, exit_, (previous_exit, len(days)))
    _check_intent_volumes(rows, signal, entry, exit_, terminal)
    entry_reason = item.get("entry_reason")
    if entry_reason is not None and (not isinstance(entry_reason, str) or not entry_reason):
        raise W0Error("Некорректная причина входа carry.")
    return (
        {
            "side": "short_spread",
            "entry_date": days[entry],
            "exit_date": days[exit_],
            "signal_date": days[signal],
            "reason": reason,
            "open_at_end": terminal,
            **({"entry_reason": entry_reason} if entry_reason is not None else {}),
        },
        exit_,
    )


def carry_settlement_intents(
    plugin: StrategyPlugin, window: WindowSnapshot, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """Validate short-near/long-far settlement decisions against the study prefix."""
    if (
        not isinstance(window, WindowSnapshot)
        or window.universe != PAIR_UNIVERSE
        or window.timeframe != "1d"
        or "same_day_settlement" not in plugin.supported_fills
        or not isinstance(params, dict)
        or not isinstance(window.carry_study, Mapping)
    ):
        raise W0Error("Некорректное окно или исполнение carry-стратегии.")
    raw_rows = window.carry_study.get("rows")
    days = _validate_study_rows(raw_rows)
    assert isinstance(raw_rows, list)
    indices = {d: i for i, d in enumerate(days)}
    result = plugin.evaluate_intents(deepcopy(window), deepcopy(params), "same_day_settlement")
    if (
        not isinstance(result, StrategyIntentRun)
        or result.strategy_id != plugin.strategy_id
        or result.fill != "same_day_settlement"
        or result.parameters != params
        or not isinstance(result.intents, tuple)
    ):
        raise W0Error("Плагин вернул несогласованный набор сигналов carry.")
    trades: list[dict[str, Any]] = []
    previous_exit = -1
    for item in result.intents:
        trade, previous_exit = _validate_carry_intent(item, raw_rows, days, indices, previous_exit)
        trades.append(trade)
    return trades
