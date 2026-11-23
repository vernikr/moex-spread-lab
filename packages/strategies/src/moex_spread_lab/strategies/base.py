"""Shared strategy math, fill helpers, and calendar buffer/scan utilities."""

from __future__ import annotations

import importlib
import importlib.metadata
import math
from array import array
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, cast

from moex_spread_lab.core._fastcore_loader import load_fastcore
from moex_spread_lab.core.contracts import (
    CompatibilityVerdict,
    EntryBandPoint,
    InstrumentContext,
    ParameterSpec,
    StrategyIntentRun,
    StrategyPlugin,
    WindowSnapshot,
)
from moex_spread_lab.core.settings import MIN_DAILY_VOLUME, ResearchSettings
from moex_spread_lab.core.w0 import W0Error

from .registry import (
    StrategyRegistry,
    calendar_proof_plugin_ids,
    carry_proof_plugin_ids,
    daily_pair_proof_plugin_ids,
    discover_strategies,
)

_importlib_metadata = importlib.metadata

PRICE_FILLS: dict[str, tuple[int, str]] = {
    "next_open": (1, "open"),
    "next_close": (1, "close"),
    "reference_same_bar": (0, "close"),
}
CALENDAR_FILLS: tuple[tuple[str, int, str], ...] = (
    ("reference_same_bar", 0, "close"),
    ("next_close", 1, "close"),
    ("next_open", 1, "open"),
)
REFERENCE_WARNING = (
    "Справочный режим: сигнал и цена закрытия одного бара. После расчёта сигнала "
    "эта цена уже известна; результат не является проверкой исполнимой доходности."
)
ENTRY_BAND_NOTE_RU = (
    "Пороги входа по закрытию сигнального бара: покупка спреда при цене не выше "
    "нижней линии, продажа — не ниже верхней. Только при отсутствии позиции и "
    "пройденных ограничениях; выходы используют собственные правила стратегии. "
    "Это не цены фактического исполнения."
)
NON_PRICE_SIGNAL_NOTE_RU = (
    "У стратегии фондирования нет двух ценовых порогов: вход определяется историей ставок, "
    "а не пересечением цены спреда."
)

ZSCORE_CALENDAR_OPTIONS: dict[str, dict[str, int]] = {
    "quarterly": {"window": 20, "max_hold_days": 30, "close_before": 7},
    "monthly": {"window": 7, "max_hold_days": 10, "close_before": 3},
}

_EPOCH_DT = datetime(1970, 1, 1)
_EPOCH_DATE = date(1970, 1, 1)
CALENDAR_EXIT_REASONS = ("z_reverted", "max_hold", "near_expiry_buffer", "end_of_window")


def population_rolling_stats(values: list[float]) -> tuple[float, float]:
    """Population mean and standard deviation (used by ordinary pair mean reversion)."""
    n = len(values)
    mean = sum(values) / n
    variance = sum([(value - mean) ** 2 for value in values]) / n
    return mean, math.sqrt(variance)


def calendar_price(row: Mapping[str, Any], fill: str) -> float:
    return float(row["open_spread"] if fill == "open" else row["spread"])


def make_calendar_trade(
    entry: Mapping[str, Any],
    exit_row: Mapping[str, Any],
    direction: int,
    fill: str,
    reason: str,
    signal_info: tuple[str, Mapping[str, Any], float],
) -> dict[str, Any]:
    signal_kind, signal_row, signal_value = signal_info
    return {
        "entry": entry,
        "exit": exit_row,
        "direction": direction,
        "reason": reason,
        "entry_price": calendar_price(entry, fill),
        "exit_price": calendar_price(exit_row, fill),
        "signal": {"kind": signal_kind, "date": signal_row["date"], "value": signal_value},
    }


def elapsed_calendar_days(start: str, end: str) -> float:
    if "T" in start or "T" in end:
        left = datetime.fromisoformat(str(start).replace("Z", ""))
        right = datetime.fromisoformat(str(end).replace("Z", ""))
        return (right - left).total_seconds() / 86400
    return float((date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days)


def row_ts_days(raw_date: Any) -> float:
    s = str(raw_date)
    if "T" in s:
        return (datetime.fromisoformat(s.replace("Z", "")) - _EPOCH_DT).total_seconds() / 86400.0
    return float((date.fromisoformat(s[:10]) - _EPOCH_DATE).days)


def calendar_row_buffers(
    rows: list[dict[str, Any]],
    *,
    z_cache: dict[Any, Any] | None = None,
    z_cache_key: Any = None,
) -> tuple[Any, ...]:
    """Build typed C-contiguous column arrays for fast calendar strategy scans."""
    cache_key = (
        ("_cols", z_cache_key) if (z_cache is not None and z_cache_key is not None) else None
    )
    if cache_key is not None and z_cache is not None and cache_key in z_cache:
        return z_cache[cache_key]
    n = len(rows)
    spreads = array("d", (float(r["spread"]) for r in rows))
    dev_pp = array("d", (float(r.get("dev_pp", math.nan)) for r in rows))
    ts_days = array("d", (row_ts_days(r["date"]) for r in rows))
    dte = array("i", (int(r["days_to_near_expiry"]) for r in rows))
    sig_el = array("B", (1 if r.get("signal_eligible", True) else 0 for r in rows))
    ent_el = array("B", (1 if r.get("entry_eligible", True) else 0 for r in rows))
    ext_el = array("B", (1 if r.get("exit_eligible", True) else 0 for r in rows))
    elig_pref = array("i", [0] * (n + 1))
    acc = 0
    for idx in range(n):
        acc += sig_el[idx]
        elig_pref[idx + 1] = acc
    cols = (spreads, dev_pp, ts_days, dte, sig_el, ent_el, ext_el, elig_pref)
    if cache_key is not None and z_cache is not None:
        z_cache[cache_key] = cols
    return cols


def check_calendar_liquidity(
    window: WindowSnapshot, settings: ResearchSettings | None
) -> tuple[CompatibilityVerdict | None, list[dict[str, Any]]]:
    """Shared 1h/1d calendar window liquidity check for calendar plugins."""
    if window.timeframe == "1h":
        eligible = [
            r for r in window.rows if r.get("volume_eligible", (r.get("min_volume") or 0) > 0)
        ]
        if len(eligible) < 2:
            return (
                CompatibilityVerdict(
                    False, "fewer than two causally liquidity-eligible hourly observations"
                ),
                [],
            )
        if (
            settings is not None
            and not settings.allow_failed_volume
            and not window.extra.get("tradable_every_session", True)
        ):
            return (
                CompatibilityVerdict(False, "Не пройдено правило ликвидности на обучении."),
                [],
            )
        return None, eligible
    eligible = [r for r in window.rows if (r.get("min_volume") or 0) > 0]
    if len(eligible) < 2:
        return (
            CompatibilityVerdict(
                False, "fewer than two positive-volume calendar-spread observations"
            ),
            [],
        )
    if (
        settings is not None
        and not settings.allow_failed_volume
        and min(float(r["min_volume"]) for r in eligible) < MIN_DAILY_VOLUME
    ):
        return (
            CompatibilityVerdict(False, "Не пройдено правило ликвидности на обучении."),
            [],
        )
    return None, eligible


def validate_parameter_map(
    raw: Any,
    schema: tuple[ParameterSpec, ...],
) -> dict[str, Any]:
    """Validate and merge raw parameter overrides against a ``ParameterSpec`` schema."""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise W0Error("Переданы параметры другой стратегии")
    raw = cast(Mapping[str, Any], raw)
    by_name = {spec.name: spec for spec in schema}
    if set(raw.keys()) - set(by_name.keys()):
        raise W0Error("Переданы параметры другой стратегии")
    resolved: dict[str, Any] = {spec.name: spec.default for spec in schema}
    for name, value in raw.items():
        spec = by_name[name]
        if not spec.contains(value):
            raise W0Error(f"{name}: допустимый диапазон {spec.min_value}–{spec.max_value}")
        resolved[name] = int(value) if spec.kind == "int" else float(value)
    if (
        "entry_z" in resolved
        and "exit_z" in resolved
        and float(resolved["exit_z"]) >= float(resolved["entry_z"])
    ):
        raise W0Error("Порог выхода должен быть ниже порога входа")
    return resolved


def clean_nonfinite_json(value: Any) -> Any:
    """Replace non-finite floats with None (C99 fastcore + pure-Python fallback)."""
    fc = load_fastcore()
    if fc is not None:
        return fc.clean_obj(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean_nonfinite_json(v) for k, v in cast(dict[Any, Any], value).items()}
    if isinstance(value, (list, tuple)):
        return [clean_nonfinite_json(v) for v in cast(list[Any] | tuple[Any, ...], value)]
    return value


__all__ = [
    "CALENDAR_EXIT_REASONS",
    "CALENDAR_FILLS",
    "ENTRY_BAND_NOTE_RU",
    "NON_PRICE_SIGNAL_NOTE_RU",
    "PRICE_FILLS",
    "REFERENCE_WARNING",
    "ZSCORE_CALENDAR_OPTIONS",
    "CompatibilityVerdict",
    "EntryBandPoint",
    "InstrumentContext",
    "ParameterSpec",
    "StrategyIntentRun",
    "StrategyPlugin",
    "StrategyRegistry",
    "WindowSnapshot",
    "calendar_price",
    "calendar_proof_plugin_ids",
    "calendar_row_buffers",
    "carry_proof_plugin_ids",
    "check_calendar_liquidity",
    "clean_nonfinite_json",
    "daily_pair_proof_plugin_ids",
    "discover_strategies",
    "elapsed_calendar_days",
    "importlib",
    "make_calendar_trade",
    "population_rolling_stats",
    "row_ts_days",
    "validate_parameter_map",
]
