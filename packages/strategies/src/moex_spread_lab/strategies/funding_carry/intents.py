"""Fail-closed validation of funding-carry strategy intents.

This validates decisions only; proof separately owns evidence and accounting.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.core.contracts import StrategyPlugin, WindowSnapshot
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error, load_contracts

from ..base import build_date_index, evaluate_plugin_intents, require_finite_number

_NO_STUDY_ROWS = "Нет пригодного ряда расчётных цен carry."
_BAD_STUDY = "Некорректные расчётные цены или порядок дат carry."
_BAD_HORIZON = "Carry-сделка нарушает экспирацию или окно поставки."
_UNSET = object()


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


def _validate_study_row(row: Mapping[str, Any], *, full_study: bool, last_day: str) -> None:
    require_finite_number(row.get("near_settle"), _BAD_STUDY, positive=True)
    require_finite_number(row.get("far_settle"), _BAD_STUDY, positive=True)
    if full_study:
        require_finite_number(row.get("daily_funding_received_short_near"), _BAD_STUDY)
        require_finite_number(row.get("daily_gross_carry_pnl"), _BAD_STUDY)
    for col in ("daily_funding_received_short_near", "daily_gross_carry_pnl", "near_swaprate"):
        if col in row:
            require_finite_number(row.get(col), _BAD_STUDY)
    known_raw = row.get("funding_known_on")
    if known_raw is not None:
        known_on = _parse_iso_day(known_raw)
        if known_on < str(row["date"]) or known_on > last_day:
            raise W0Error("Оценка фандинга выходит за границу окна доказательства.")


def _validate_study_rows(
    study: Mapping[str, Any], rows: list[Mapping[str, Any]], last_day: str
) -> None:
    full_study = "schema_version" in study
    for row in rows:
        _validate_study_row(row, full_study=full_study, last_day=last_day)


def _expiry_from_contracts_path(
    window: WindowSnapshot, study: Mapping[str, Any], near: str | None, far: str
) -> Any:
    cpath = window.extra.get("contracts_path")
    if isinstance(cpath, Path):
        contracts, _ = load_contracts(cpath, str(near or far), far)
        return contracts[far].get("expiry")
    prov = study.get("provenance")
    if isinstance(prov, Mapping):
        prov_path: Any = cast(Mapping[str, Any], prov).get("contracts_path")
        if isinstance(prov_path, str):
            path = Path(prov_path)
            if path.is_file():
                contracts, _ = load_contracts(path, str(near or far), far)
                return contracts[far].get("expiry")
    return _UNSET


def _resolve_horizon(
    window: WindowSnapshot, study: Mapping[str, Any], parsed_dates: list[date]
) -> tuple[date, date] | None:
    far = window.far or (study["far"] if isinstance(study.get("far"), str) else None)
    near = window.near or (study["near"] if isinstance(study.get("near"), str) else None)
    raw_exp: Any = _UNSET
    if far and isinstance(window.contracts.get(far), Mapping) and "expiry" in window.contracts[far]:
        raw_exp = window.contracts[far]["expiry"]
    elif "far_expiry" in window.extra:
        raw_exp = window.extra["far_expiry"]
    elif "far_expiry" in study:
        raw_exp = study["far_expiry"]
    elif far:
        raw_exp = _expiry_from_contracts_path(window, study, near, far)
    if raw_exp is _UNSET:
        return None
    expiry = date.fromisoformat(_parse_iso_day(raw_exp))
    buf = window.extra.get("expiry_buffer_days", 5)
    win = window.extra.get("assignment_window_trading_days", 10)
    if (
        isinstance(buf, bool)
        or not isinstance(buf, int)
        or buf < 0
        or isinstance(win, bool)
        or not isinstance(win, int)
        or win < 1
    ):
        raise W0Error(_BAD_HORIZON)
    observed = [d for d in parsed_dates if d < expiry]
    assignment = (
        observed[-win]
        if observed and observed[-1] >= expiry - timedelta(days=15) and len(observed) >= win
        else None
    )
    cutoff = expiry - timedelta(days=buf)
    return expiry, (min(cutoff, assignment) if assignment is not None else cutoff)


def _check_carry_horizon(
    entry: int,
    exit_: int,
    terminal: bool,
    parsed_dates: list[date],
    horizon: tuple[date, date] | None,
) -> None:
    if horizon is None:
        return
    expiry, cutoff = horizon
    invalid_term = (
        parsed_dates[exit_] >= cutoff
        if terminal
        else (
            parsed_dates[exit_] > cutoff and any(day >= cutoff for day in parsed_dates[entry:exit_])
        )
    )
    if parsed_dates[entry] >= cutoff or parsed_dates[exit_] >= expiry or invalid_term:
        raise W0Error(_BAD_HORIZON)


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
    context: tuple[list[date], tuple[date, date] | None],
) -> tuple[dict[str, Any], int]:
    try:
        entry, exit_, signal = (
            indices[trade[k]] for k in ("entry_date", "exit_date", "signal_date")
        )
        side, reason, terminal = trade["side"], trade["reason"], trade["open_at_end"]
    except (KeyError, TypeError):
        raise W0Error("Даты или поля carry-сделки отсутствуют в окне.") from None
    _check_carry_order(side, reason, terminal, (signal, entry, exit_), (previous_exit, len(days)))
    parsed_dates, horizon = context
    _check_carry_horizon(entry, exit_, terminal, parsed_dates, horizon)
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
    _validate_study_rows(study, rows, days[-1])
    parsed_dates = [date.fromisoformat(d) for d in days]
    horizon = _resolve_horizon(window, study, parsed_dates)
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
        trade, previous_exit = _validate_carry_intent(
            item, rows, days, indices, previous_exit, (parsed_dates, horizon)
        )
        trades.append(trade)
    return trades
