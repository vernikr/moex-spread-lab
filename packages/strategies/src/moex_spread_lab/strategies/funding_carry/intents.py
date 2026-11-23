"""Fail-closed validation of funding-carry strategy intents.

This validates decisions only; proof separately owns evidence and accounting.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from datetime import date
from typing import Any, cast

from moex_spread_lab.core.contracts import StrategyIntentRun, StrategyPlugin, WindowSnapshot
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

_NO_STUDY_ROWS = "Нет пригодного ряда расчётных цен carry."


def _is_pos_finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value)) and float(value) > 0


def _carry_rows(study: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the carry-study rows as typed mappings; a malformed prefix is unusable."""
    rows: Any = study.get("rows")
    if not isinstance(rows, list) or not rows:
        raise W0Error(_NO_STUDY_ROWS)
    typed: list[Mapping[str, Any]] = []
    for row in cast(list[Any], rows):
        if not isinstance(row, Mapping):
            raise W0Error(_NO_STUDY_ROWS)
        typed.append(cast(Mapping[str, Any], row))
    return typed


def _validate_study_rows(rows: list[Mapping[str, Any]]) -> list[str]:
    try:
        days = [row["date"] for row in rows]
        if any(
            not isinstance(day, str) or len(day) != 10 or date.fromisoformat(day).isoformat() != day
            for day in days
        ) or days != sorted(set(days)):
            raise ValueError
        for row in rows:
            if not _is_pos_finite(row["near_settle"]) or not _is_pos_finite(row["far_settle"]):
                raise ValueError
    except (ValueError, TypeError, OverflowError, KeyError):
        raise W0Error("Некорректные расчётные цены или порядок дат carry.") from None
    return cast(list[str], days)


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
    trade = cast(Mapping[str, Any], item)
    try:
        entry, exit_, signal = (
            indices[trade[k]] for k in ("entry_date", "exit_date", "signal_date")
        )
        side, reason, terminal = trade["side"], trade["reason"], trade["open_at_end"]
    except (KeyError, TypeError):
        raise W0Error("Даты или поля carry-сделки отсутствуют в окне.") from None
    _check_intent_order(side, reason, terminal, signal, entry, exit_, (previous_exit, len(days)))
    _check_intent_volumes(rows, signal, entry, exit_, terminal)
    entry_reason: Any = trade.get("entry_reason")
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
    study: Mapping[str, Any] | None = window.carry_study
    if (
        window.universe != PAIR_UNIVERSE
        or window.timeframe != "1d"
        or "same_day_settlement" not in plugin.supported_fills
        or not isinstance(study, Mapping)
    ):
        raise W0Error("Некорректное окно или исполнение carry-стратегии.")
    rows = _carry_rows(study)
    days = _validate_study_rows(rows)
    indices = {d: i for i, d in enumerate(days)}
    result: Any = plugin.evaluate_intents(deepcopy(window), deepcopy(params), "same_day_settlement")
    if (
        not isinstance(result, StrategyIntentRun)
        or result.strategy_id != plugin.strategy_id
        or result.fill != "same_day_settlement"
        or result.parameters != params
    ):
        raise W0Error("Плагин вернул несогласованный набор сигналов carry.")
    intents: Any = result.intents
    if not isinstance(intents, tuple):
        raise W0Error("Плагин вернул несогласованный набор сигналов carry.")
    trades: list[dict[str, Any]] = []
    previous_exit = -1
    for item in cast(tuple[Any, ...], intents):
        trade, previous_exit = _validate_carry_intent(item, rows, days, indices, previous_exit)
        trades.append(trade)
    return trades
