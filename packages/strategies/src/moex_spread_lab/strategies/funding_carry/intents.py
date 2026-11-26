"""Fail-closed validation of funding-carry strategy intents.

This validates decisions only; proof separately owns evidence and accounting.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, cast

from moex_spread_lab.core.contracts import StrategyPlugin, WindowSnapshot
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from ..base import build_date_index, evaluate_plugin_intents, require_finite_number

_NO_STUDY_ROWS = "Нет пригодного ряда расчётных цен carry."
_BAD_STUDY = "Некорректные расчётные цены или порядок дат carry."


def _parse_iso_day(value: Any) -> str:
    try:
        if (
            isinstance(value, str)
            and len(value) == 10
            and date.fromisoformat(value).isoformat() == value
        ):
            return value
    except ValueError:
        pass
    raise W0Error(_BAD_STUDY)


def _carry_rows(study: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the carry-study rows as typed mappings; a malformed prefix is unusable."""
    rows: Any = study.get("rows")
    if not isinstance(rows, list) or not rows:
        raise W0Error(_NO_STUDY_ROWS)
    raw = cast(list[Any], rows)
    if any(not isinstance(row, Mapping) for row in raw):
        raise W0Error(_NO_STUDY_ROWS)
    return cast(list[Mapping[str, Any]], raw)


def _check_carry_order(
    side: Any,
    reason: Any,
    terminal: Any,
    span: tuple[int, int, int],
    bounds: tuple[int, int],
) -> None:
    signal, entry, exit_ = span
    previous_exit, total_days = bounds
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


def _validate_carry_intent(
    trade: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    days: list[str],
    indices: dict[str, int],
    previous_exit: int,
) -> tuple[dict[str, Any], int]:
    try:
        entry, exit_, signal = (
            indices[trade[k]] for k in ("entry_date", "exit_date", "signal_date")
        )
        side, reason, terminal = trade["side"], trade["reason"], trade["open_at_end"]
    except (KeyError, TypeError):
        raise W0Error("Даты или поля carry-сделки отсутствуют в окне.") from None
    _check_carry_order(side, reason, terminal, (signal, entry, exit_), (previous_exit, len(days)))
    for i in (signal, entry) if terminal else (signal, entry, exit_):
        for col in ("near_volume", "far_volume"):
            require_finite_number(
                rows[i].get(col),
                "Недостаточные данные объёмов для carry-сделки.",
                positive=True,
            )
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
    days, indices = build_date_index(rows, _BAD_STUDY, parse=_parse_iso_day)
    for row in rows:
        for col in ("near_settle", "far_settle"):
            require_finite_number(row.get(col), _BAD_STUDY, positive=True)
    intents = evaluate_plugin_intents(
        plugin,
        window,
        params,
        "same_day_settlement",
        msg="Плагин вернул несогласованный набор сигналов carry.",
    )
    trades: list[dict[str, Any]] = []
    previous_exit = -1
    for item in intents:
        trade, previous_exit = _validate_carry_intent(item, rows, days, indices, previous_exit)
        trades.append(trade)
    return trades
