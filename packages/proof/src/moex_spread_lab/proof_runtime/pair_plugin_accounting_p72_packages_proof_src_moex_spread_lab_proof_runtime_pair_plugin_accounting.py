"""Typed row validation and pricing helpers for P7.2 pair-plugin accounting."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any, cast

from moex_spread_lab.core.contracts import WindowSnapshot
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import W0Error


def is_runtime_instance_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
    value: object, expected_type: type[object]
) -> bool:
    return isinstance(value, expected_type)


def has_non_mapping_rows_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
    rows: Iterable[object],
) -> bool:
    return any(not isinstance(row, Mapping) for row in rows)


def eligible_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
    row: Mapping[str, Any],
) -> bool:
    values = [row.get("near_volume"), row.get("far_volume")]
    if any(
        value is not None
        and (type(value) not in (int, float) or not math.isfinite(value) or value < 0)
        for value in values
    ):
        raise ValueError
    return all(value is not None and value > 0 for value in values)


def valid_date_key_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
    key: object, daily: bool
) -> bool:
    if not isinstance(key, str):
        return False
    if daily:
        return len(key) == 10 and date.fromisoformat(key).isoformat() == key
    return len(key) == 19 and datetime.fromisoformat(key).isoformat(timespec="seconds") == key


def validated_rows_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
    window: WindowSnapshot,
    params: dict[str, Any],
    benchmark_marks: Mapping[str, float],
    settings: ResearchSettings,
    through: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    is_instance = is_runtime_instance_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting
    window_input: object = window
    if not is_instance(window_input, WindowSnapshot):
        raise W0Error("Для расчёта требуется корректное дневное окно пары.")
    params_input: object = params
    marks_input: object = benchmark_marks
    settings_input: object = settings
    if (
        window.timeframe not in ("1d", "1h")
        or not is_instance(settings_input, ResearchSettings)
        or settings.timeframe != window.timeframe
        or not is_instance(params_input, dict)
        or not window.near
        or not window.far
        or window.near == window.far
        or not is_instance(marks_input, Mapping)
        or not window.rows
        or has_non_mapping_rows_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
            window.rows
        )
    ):
        raise W0Error("Для расчёта требуется корректное дневное окно пары.")
    try:
        dates: list[str] = [row["date"] for row in window.rows]
        if (
            any(
                not valid_date_key_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
                    day, window.timeframe == "1d"
                )
                for day in dates
            )
            or dates != sorted(set(dates))
            or (
                through is not None
                and not valid_date_key_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
                    through, len(through) == 10
                )
            )
        ):
            raise ValueError
        prefix: list[dict[str, Any]] = [
            row
            for row in window.rows
            if through is None
            or (row["date"][:10] if len(through) == 10 else row["date"]) <= through
        ]

        analysis_rows = [
            row
            for row in prefix
            if eligible_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
                row
            )
        ]
        if len(analysis_rows) < 2:
            raise ValueError
    except (ValueError, KeyError, TypeError, OverflowError):
        raise W0Error("Некорректный дневной период или объём пары.") from None
    return prefix, analysis_rows


def positive_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
    value: Any,
) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError
    return float(value)


def priced_rows_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
    window: WindowSnapshot,
    prefix: list[dict[str, Any]],
    benchmark_marks: Mapping[str, float],
) -> tuple[list[dict[str, Any]], float]:
    positive = positive_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting
    try:
        legs: list[Mapping[str, Any]] = [
            cast(Mapping[str, Any], window.contracts[window.near]),
            cast(Mapping[str, Any], window.contracts[window.far]),
        ]
        multipliers: list[float] = [
            positive(contract["tick_value"]) / positive(contract["tick_size"]) for contract in legs
        ]
        lot = positive(legs[0]["lot_size"])
        if not all(math.isfinite(mult) and mult > 0 for mult in multipliers):
            raise ValueError
        rows: list[dict[str, Any]] = []
        for raw in prefix:
            near, far = positive(raw["near_close"]), positive(raw["far_close"])
            near_open, far_open = positive(raw["near_open"]), positive(raw["far_open"])
            spread = near * multipliers[0] - far * multipliers[1]
            if not math.isfinite(spread) or not math.isfinite(
                near_open * multipliers[0] - far_open * multipliers[1]
            ):
                raise ValueError
            positive(benchmark_marks[raw["date"]])
            rows.append(
                {
                    "date": raw["date"],
                    "timestamp": raw.get("timestamp", raw["date"]),
                    "session_date": raw.get("session_date", raw["date"]),
                    "benchmark_mark_date": raw.get("benchmark_mark_date"),
                    "near": near,
                    "far": far,
                    "near_open": near_open,
                    "far_open": far_open,
                    "spread": round(spread, 8),
                    "volume_eligible": eligible_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
                        raw
                    ),
                }
            )
    except (TypeError, KeyError, ValueError, OverflowError, ZeroDivisionError):
        raise W0Error("Нет достоверных цен или параметров обеих ног пары.") from None
    return rows, lot
