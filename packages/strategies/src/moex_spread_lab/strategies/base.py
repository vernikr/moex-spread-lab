"""Shared strategy math, fill helpers, and calendar buffer/scan utilities."""

from __future__ import annotations

import math
from array import array
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, cast

from moex_spread_lab.core._fastcore_loader import load_fastcore
from moex_spread_lab.core.contracts import (
    CompatibilityVerdict,
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
    variance = sum((value - mean) ** 2 for value in values) / n
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


def _build_calendar_buffers(rows: list[dict[str, Any]]) -> tuple[Any, ...]:
    n = len(rows)
    spreads, dev_pp, ts_days = array("d"), array("d"), array("d")
    dte, sig_el, ent_el, ext_el = array("i"), array("B"), array("B"), array("B")
    elig_pref = array("i", [0] * (n + 1))
    acc = 0
    for idx, r in enumerate(rows):
        spreads.append(float(r["spread"]))
        dev_pp.append(float(r.get("dev_pp", math.nan)))
        ts_days.append(row_ts_days(r["date"]))
        dte.append(int(r["days_to_near_expiry"]))
        s_flag = int(bool(r.get("signal_eligible", True)))
        sig_el.append(s_flag)
        ent_el.append(int(bool(r.get("entry_eligible", True))))
        ext_el.append(int(bool(r.get("exit_eligible", True))))
        acc += s_flag
        elig_pref[idx + 1] = acc
    return (spreads, dev_pp, ts_days, dte, sig_el, ent_el, ext_el, elig_pref)


def calendar_row_buffers(
    rows: list[dict[str, Any]],
    *,
    z_cache: dict[Any, Any] | None = None,
    z_cache_key: Any = None,
) -> tuple[Any, ...]:
    """Build typed C-contiguous column arrays for fast calendar strategy scans."""
    if z_cache is not None and z_cache_key is not None:
        cache_key = ("_cols", z_cache_key)
        if cache_key in z_cache:
            return z_cache[cache_key]
        cols = _build_calendar_buffers(rows)
        z_cache[cache_key] = cols
        return cols
    return _build_calendar_buffers(rows)


def check_calendar_liquidity(
    window: WindowSnapshot, settings: ResearchSettings | None
) -> tuple[CompatibilityVerdict | None, list[dict[str, Any]]]:
    """Shared 1h/1d calendar window liquidity check for calendar plugins."""
    hourly = window.timeframe == "1h"
    if hourly:
        eligible = [
            r for r in window.rows if r.get("volume_eligible", (r.get("min_volume") or 0) > 0)
        ]
        too_few = "fewer than two causally liquidity-eligible hourly observations"
    else:
        eligible = [r for r in window.rows if (r.get("min_volume") or 0) > 0]
        too_few = "fewer than two positive-volume calendar-spread observations"
    if len(eligible) < 2:
        return CompatibilityVerdict(False, too_few), []
    if settings is not None and not settings.allow_failed_volume:
        failed_volume = (
            not window.extra.get("tradable_every_session", True)
            if hourly
            else min(float(r["min_volume"]) for r in eligible) < MIN_DAILY_VOLUME
        )
        if failed_volume:
            return CompatibilityVerdict(False, "Не пройдено правило ликвидности на обучении."), []
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


def require_finite_number(
    value: Any,
    msg: str = "Требуется конечное числовое значение.",
    *,
    positive: bool = False,
) -> float:
    """Validate that ``value`` is a finite int/float (excluding bool)."""
    try:
        if type(value) not in (int, float):
            raise ValueError
        num = float(value)
        if not math.isfinite(num) or (positive and num <= 0):
            raise ValueError
        return num
    except (OverflowError, ValueError):
        raise W0Error(msg) from None


def build_date_index(
    rows: Sequence[Mapping[str, Any]],
    msg: str,
    parse: Callable[[Any], str] | None = None,
) -> tuple[list[str], dict[str, int]]:
    """Verify strictly increasing unique row dates and return ``(days, index)``."""
    if not rows:
        raise W0Error(msg)
    if parse is None:
        raw = [r.get("date") for r in rows]
        if any(not isinstance(d, str) or not d for d in raw):
            raise W0Error(msg)
        days = cast(list[str], raw)
    else:
        days = [parse(r.get("date")) for r in rows]
    if days != sorted(set(days)):
        raise W0Error(msg)
    return days, {day: i for i, day in enumerate(days)}


def evaluate_plugin_intents(
    plugin: StrategyPlugin,
    window: WindowSnapshot,
    params: dict[str, Any],
    fill: str,
    *,
    isolate: bool = True,
    msg: str = "Плагин вернул несогласованный набор сигналов.",
    tuple_msg: str | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """Run ``plugin.evaluate_intents`` and verify its ``StrategyIntentRun`` envelope."""
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
        raise W0Error(msg)
    intents: Any = result.intents
    if not isinstance(intents, tuple):
        raise W0Error(tuple_msg or msg)
    items = cast(tuple[Any, ...], intents)
    if any(not isinstance(item, Mapping) for item in items):
        raise W0Error("Некорректный сигнал плагина.")
    return cast(tuple[Mapping[str, Any], ...], items)


def validate_signal_payload(
    item: Mapping[str, Any],
    *,
    forbidden_reasons: frozenset[str],
) -> tuple[str, str, Mapping[str, Any]]:
    """Validate common ``side``, ``reason``, and ``signal`` fields of a spread intent."""
    side, reason, signal = item.get("side"), item.get("reason"), item.get("signal")
    if (
        side not in ("long_spread", "short_spread")
        or not isinstance(reason, str)
        or not reason
        or reason in forbidden_reasons
    ):
        raise W0Error("Некорректное направление или причина выхода.")
    if not isinstance(signal, Mapping):
        raise W0Error("Сигнал плагина должен содержать тип и дату.")
    sig = cast(Mapping[str, Any], signal)
    if not isinstance(sig.get("kind"), str) or not sig["kind"]:
        raise W0Error("Сигнал плагина должен содержать тип и дату.")
    require_finite_number(sig.get("value"), "Значение сигнала должно быть конечным числом.")
    return side, reason, sig


@dataclass
class IntentSpanTracker:
    """Track non-overlapping causal trade spans and bar eligibility."""

    rows: list[dict[str, Any]]
    delay: int
    previous_exit: int = -1
    entry_days: set[str] = field(default_factory=set)
    exit_days: set[str] = field(default_factory=set)

    def _check_order(
        self, indices: tuple[int, int, int], days: tuple[str, str], terminal_open: bool
    ) -> None:
        si, ei, xi = indices
        entry_day, exit_day = days
        if (
            si < 0
            or si > ei - self.delay
            or (xi <= ei and not terminal_open)
            or ei < self.previous_exit
            or entry_day in self.entry_days
            or exit_day in self.exit_days
        ):
            raise W0Error("Нарушен порядок событий или причинность сигналов.")

    def check_and_advance(
        self,
        indices: tuple[int, int, int],
        days: tuple[str, str],
        *,
        forced: bool,
        terminal_open: bool = False,
    ) -> None:
        self._check_order(indices, days, terminal_open)
        si, ei, xi = indices
        entry_day, exit_day = days
        last = len(self.rows) - 1
        if (terminal_open and (xi != last or ei != xi)) or (forced and xi != last):
            raise W0Error("Некорректная конечная отметка позиции.")
        if not self.rows[si].get("signal_eligible", True) or not self.rows[ei].get(
            "entry_eligible", True
        ):
            raise W0Error("Сигнал или вход на недопустимом баре.")
        if not forced and not terminal_open and not self.rows[xi].get("exit_eligible", True):
            raise W0Error("Выход на недопустимом баре.")
        self.entry_days.add(entry_day)
        self.exit_days.add(exit_day)
        self.previous_exit = xi


__all__ = [
    "CALENDAR_EXIT_REASONS",
    "CALENDAR_FILLS",
    "ENTRY_BAND_NOTE_RU",
    "NON_PRICE_SIGNAL_NOTE_RU",
    "PRICE_FILLS",
    "REFERENCE_WARNING",
    "ZSCORE_CALENDAR_OPTIONS",
    "IntentSpanTracker",
    "StrategyRegistry",
    "build_date_index",
    "calendar_price",
    "calendar_proof_plugin_ids",
    "calendar_row_buffers",
    "carry_proof_plugin_ids",
    "check_calendar_liquidity",
    "clean_nonfinite_json",
    "daily_pair_proof_plugin_ids",
    "discover_strategies",
    "elapsed_calendar_days",
    "evaluate_plugin_intents",
    "make_calendar_trade",
    "population_rolling_stats",
    "require_finite_number",
    "row_ts_days",
    "validate_parameter_map",
    "validate_signal_payload",
]
