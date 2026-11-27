"""Internal pair-funding replay primitive; not a complete proof adapter.

Funding is applied after the bar's executable entry/exit, but before a terminal
non-executable close mark. Missing rates remain unknown, never a zero charge.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence, Set
from numbers import Real
from typing import TYPE_CHECKING, Any, Literal, TypedDict, TypeGuard, cast

from moex_spread_lab.core.w0 import W0Error

if TYPE_CHECKING:
    from moex_spread_lab.strategies.funding_carry.proof_funding import (
        FundingEvidence,
        FundingWindow,
    )

_FundingStatus = Literal["no_clearing", "not_held", "missing_rate", "applied"]


class _FundingRow(TypedDict):
    date: str
    position_at_clearing: int
    funding_rub: float | None
    funding_status: _FundingStatus


class _DailyFundingRow(_FundingRow):
    rate_kind: str
    rate: float | None


class _DailyFundingReplay(TypedDict):
    daily: list[_DailyFundingRow]
    provenance: dict[str, Any]


class _AttachedFunding(TypedDict):
    series: list[dict[str, object]]
    provenance: dict[str, Any]


def _is_string(value: object) -> TypeGuard[str]:
    return isinstance(value, str)


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def _is_list_or_tuple(value: object) -> bool:
    return isinstance(value, (list, tuple))


def _is_instance(value: object, instance_type: type[object]) -> bool:
    return isinstance(value, instance_type)


def _finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _positions(
    dates: Sequence[str], trades: Sequence[Mapping[str, object]], index: Mapping[str, int]
) -> list[int]:
    positions = [0] * len(dates)
    last_exit = -1
    for trade in trades:
        if not _is_mapping(trade):
            raise W0Error("Некорректная сделка для расчёта фондирования.")
        try:
            entry_fields = cast(Mapping[str, object], trade["entry"])
            exit_fields = cast(Mapping[str, object], trade["exit"])
            entry_date = cast(str, entry_fields["date"])
            exit_date = cast(str, exit_fields["date"])
            entry, exit_ = index[entry_date], index[exit_date]
            side, terminal = trade["side"], trade["open_at_end"]
        except (KeyError, TypeError):
            raise W0Error("Некорректная сделка для расчёта фондирования.") from None
        if (
            side not in ("long_spread", "short_spread")
            or type(terminal) is not bool
            or entry >= exit_
            or entry <= last_exit
            or (terminal and exit_ != len(dates) - 1)
        ):
            raise W0Error("Нарушен порядок позиций для расчёта фондирования.")
        position = 1 if side == "long_spread" else -1
        for i in range(entry, exit_ + int(terminal)):
            positions[i] = position
        last_exit = exit_
    return positions


def _funding_row(
    day: str,
    position: int,
    rates: Mapping[str, float | None],
    no_clearing: Set[str],
    near_lot_size: float,
    quantity: int,
) -> _FundingRow:
    rate = rates.get(day)
    status: _FundingStatus = (
        "no_clearing"
        if day in no_clearing
        else "not_held"
        if not position
        else "missing_rate"
        if rate is None
        else "applied"
    )
    try:
        value: float | None = (
            0.0
            if day in no_clearing or not position
            else None
            if rate is None
            else -position * rate * near_lot_size * quantity
        )
    except OverflowError:
        raise W0Error("Переполнение расчёта фондирования.") from None
    if value is not None and not _finite(value):
        raise W0Error("Переполнение расчёта фондирования.")
    return {
        "date": day,
        "position_at_clearing": position,
        "funding_rub": value,
        "funding_status": status,
    }


def _funding_index(
    dates: Sequence[str], rates: Mapping[str, float | None], near_lot_size: float, quantity: int
) -> dict[str, int]:
    if (
        not dates
        or any(not _is_string(day) or not day for day in dates)
        or list(dates) != sorted(set(dates))
        or not _is_mapping(rates)
        or not _finite(near_lot_size)
        or near_lot_size <= 0
        or type(quantity) is not int
        or quantity <= 0
    ):
        raise W0Error("Некорректные входные данные для расчёта фондирования.")
    return {day: i for i, day in enumerate(dates)}


def replay_pair_funding(
    dates: Sequence[str],
    trades: Sequence[Mapping[str, object]],
    rates: Mapping[str, float | None],
    *,
    near_lot_size: float,
    quantity: int,
    no_clearing_dates: Sequence[str] = (),
) -> list[_FundingRow]:
    """Return per-bar signed near-leg funding; callers must establish evidence.

    Dates are unique ordered bar keys, and rates must use those exact keys. This
    deliberately does not infer an intraday clearing timestamp from a daily rate.
    Unknown rates produce None on held bars. It computes no P&L, fees or capital.
    """

    index = _funding_index(dates, rates, near_lot_size, quantity)
    if any(
        day not in index or (rate is not None and not _finite(rate)) for day, rate in rates.items()
    ) or any(day not in index or day in rates for day in no_clearing_dates):
        raise W0Error("Некорректная ставка фондирования или дата.")
    positions = _positions(dates, trades, index)
    no_clearing = set(no_clearing_dates)
    return [
        _funding_row(day, position, rates, no_clearing, near_lot_size, quantity)
        for day, position in zip(dates, positions, strict=True)
    ]


def _validate_daily_dates(dates: Sequence[object], through: object | None) -> None:
    from datetime import date

    try:
        if any(not _is_string(day) or date.fromisoformat(day).isoformat() != day for day in dates):
            raise ValueError
        if through is not None and (
            not _is_string(through) or date.fromisoformat(through).isoformat() != through
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise W0Error("Для дневного фондирования требуются даты YYYY-MM-DD.") from None


def _resolved_marks(evidence: FundingEvidence) -> tuple[dict[str, float], list[str]]:
    no_clearing: list[str] = []
    rates: dict[str, float] = {}
    for day, mark in evidence.marks.items():
        if mark.kind == "no_clearing" and mark.value is None:
            no_clearing.append(day)
        elif mark.kind in {"published", "rechecked", "midpoint"} and mark.value is not None:
            rates[day] = mark.value
        else:
            raise W0Error("Некорректная отметка ставки фондирования.")
    return rates, no_clearing


def replay_daily_pair_funding(
    dates: Sequence[str],
    trades: Sequence[Mapping[str, object]],
    window: FundingWindow,
    *,
    near_lot_size: float,
    quantity: int,
    through: str | None = None,
) -> _DailyFundingReplay:
    """Resolve existing funding evidence for daily bars, then replay its payments.

    The window must be constructed from the same positive-volume analysis bars.
    The existing resolver enforces availability and the train/prefix cutoff; this
    function never invents intraday clearing times or fills unresolved gaps.
    """
    from copy import deepcopy

    from moex_spread_lab.strategies.funding_carry.proof_funding import FundingWindow

    if not _is_instance(window, FundingWindow) or not dates:
        raise W0Error("Для дневного расчёта нужны окно и даты фондирования.")
    _validate_daily_dates(dates, through)
    expected = [day for day in window.days if through is None or day <= through]
    if list(dates) != expected or len(set(dates)) != len(dates):
        raise W0Error("Даты баров не совпадают с разрешённым окном фондирования.")
    evidence = window.resolve(through)
    if set(evidence.marks) != set(dates):
        raise W0Error("Неполное покрытие ставок фондирования.")
    rates, no_clearing = _resolved_marks(evidence)
    base_rows = replay_pair_funding(
        dates,
        trades,
        rates,
        near_lot_size=near_lot_size,
        quantity=quantity,
        no_clearing_dates=no_clearing,
    )
    rows: list[_DailyFundingRow] = [
        {
            **row,
            "rate_kind": evidence.marks[row["date"]].kind,
            "rate": evidence.marks[row["date"]].value,
        }
        for row in base_rows
    ]
    return {"daily": rows, "provenance": deepcopy(evidence.provenance)}


_SCALARS = (str, int, float, bool, type(None))


def _isolated(row: Mapping[str, object]) -> dict[str, object]:
    """A private copy of an analysis row; flat scalar rows need no recursive copy."""
    from copy import deepcopy

    if all(isinstance(value, _SCALARS) for value in row.values()):
        return dict(row)
    return deepcopy(dict(row))


def attach_daily_unit_funding(
    series: Sequence[Mapping[str, object]],
    trades: Sequence[Mapping[str, object]],
    window: FundingWindow,
    *,
    through: str | None = None,
    near_lot_size: float,
) -> _AttachedFunding:
    """Prepare isolated unit funding fields for the existing funded sizing replay.

    This consumes only the eligible daily analysis series. It intentionally rejects
    journal-only extras and is not a complete proof or an enabled plugin adapter.
    Quantity is fixed at one here; the sizing engine applies accepted quantities.
    """
    if not _is_list_or_tuple(series) or any(not _is_mapping(row) for row in series):
        raise W0Error("Некорректные строки анализа для фондирования.")
    if any(row.get("volume_eligible", True) is not True for row in series):
        raise W0Error("В расчёт фондирования допускаются только подходящие бары анализа.")
    dates = [cast(str, row.get("date")) for row in series]
    replay = replay_daily_pair_funding(
        dates, trades, window, through=through, near_lot_size=near_lot_size, quantity=1
    )
    rows: list[dict[str, object]] = []
    for row, funding in zip(series, replay["daily"], strict=True):
        if funding["funding_rub"] is None:
            raise W0Error("Неполные данные фондирования нельзя передать в учёт.")
        # There is no contract count on an explicitly non-clearing session.
        held = bool(funding["position_at_clearing"])
        rows.append(
            {
                **_isolated(row),
                "funding_rub": funding["funding_rub"],
                "funding_contracts": int(held and funding["funding_status"] == "applied"),
                "funding_rate_kind": funding["rate_kind"],
                "funding_swaprate_rub_per_unit": funding["rate"],
            }
        )
    return {"series": rows, "provenance": replay["provenance"]}
