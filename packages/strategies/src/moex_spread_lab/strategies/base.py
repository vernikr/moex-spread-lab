"""Shared strategy math, fill helpers, and StrategyRegistry discovery for ``msl-strategies``."""

from __future__ import annotations

import importlib
import importlib.metadata
import math
import pkgutil
from array import array
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from moex_spread_lab.core.contracts import (
    CompatibilityVerdict,
    EntryBandPoint,
    InstrumentContext,
    ParameterSpec,
    StrategyIntentRun,
    StrategyPlugin,
    WindowSnapshot,
)
from moex_spread_lab.core.w0 import W0Error

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
    signal_kind: str,
    signal_row: Mapping[str, Any],
    signal_value: float,
) -> dict[str, Any]:
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
    return (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days


def row_ts_days(raw_date: Any) -> float:
    s = str(raw_date)
    if "T" in s:
        return (datetime.fromisoformat(s.replace("Z", "")) - _EPOCH_DT).total_seconds() / 86400.0
    return float((date.fromisoformat(s[:10]) - _EPOCH_DATE).days)


def calendar_row_buffers(rows: Any, *, z_cache: dict | None = None, z_cache_key: Any = None):
    """Build typed C-contiguous column arrays for fast calendar strategy scans."""
    cache_key = ("_cols", z_cache_key) if (z_cache is not None and z_cache_key is not None) else None
    if cache_key is not None and cache_key in z_cache:
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
    if cache_key is not None:
        z_cache[cache_key] = cols
    return cols


def validate_parameter_map(
    raw: Mapping[str, Any] | None,
    schema: tuple[ParameterSpec, ...],
) -> dict[str, Any]:
    """Validate and merge raw parameter overrides against a ``ParameterSpec`` schema."""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise W0Error("Переданы параметры другой стратегии")
    by_name = {spec.name: spec for spec in schema}
    unknown = set(raw.keys()) - set(by_name.keys())
    if unknown:
        raise W0Error("Переданы параметры другой стратегии")
    resolved: dict[str, Any] = {spec.name: spec.default for spec in schema}
    for name, value in raw.items():
        spec = by_name[name]
        if not spec.contains(value):
            raise W0Error(f"{name}: допустимый диапазон {spec.min_value}–{spec.max_value}")
        resolved[name] = int(value) if spec.kind == "int" else float(value)
    if "entry_z" in resolved and "exit_z" in resolved and resolved["exit_z"] >= resolved["entry_z"]:
        raise W0Error("Порог выхода должен быть ниже порога входа")
    return resolved


_NON_PLUGIN_SUBPACKAGES = frozenset({"calendar", "pair_screener", "s3_reference"})
_CANONICAL_ORDER = (
    "mean_reversion",
    "funding_only",
    "zscore_mean_reversion",
    "convergence_to_ruonia",
)


class StrategyRegistry:
    """Deterministic registry of discovered ``StrategyPlugin`` implementations."""

    def __init__(self, plugins: Mapping[str, StrategyPlugin]) -> None:
        order_index = {sid: idx for idx, sid in enumerate(_CANONICAL_ORDER)}
        ordered_ids = sorted(
            plugins.keys(),
            key=lambda sid: (order_index.get(sid, len(_CANONICAL_ORDER)), sid),
        )
        self._plugins: dict[str, StrategyPlugin] = {sid: plugins[sid] for sid in ordered_ids}

    def __contains__(self, strategy_id: object) -> bool:
        return strategy_id in self._plugins

    def __iter__(self):
        return iter(self._plugins.values())

    def __len__(self) -> int:
        return len(self._plugins)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._plugins.keys())

    def values(self) -> tuple[StrategyPlugin, ...]:
        return tuple(self._plugins.values())

    def items(self) -> tuple[tuple[str, StrategyPlugin], ...]:
        return tuple(self._plugins.items())

    def get(self, strategy_id: str) -> StrategyPlugin:
        try:
            return self._plugins[strategy_id]
        except KeyError as exc:
            raise W0Error(f"Неизвестная стратегия: {strategy_id}") from exc

    def for_universe(self, universe: str) -> tuple[StrategyPlugin, ...]:
        return tuple(p for p in self._plugins.values() if universe in p.supported_universes)

    def as_dict(self) -> dict[str, StrategyPlugin]:
        return dict(self._plugins)


def discover_strategies(*, extra_paths: tuple[Path, ...] = ()) -> StrategyRegistry:
    """Auto-discover strategy plugins via ``pkgutil.iter_modules`` and entry points."""
    strategies_dir = Path(__file__).resolve().parent

    discovered: dict[str, StrategyPlugin] = {}
    search_paths = [str(strategies_dir), *(str(p) for p in extra_paths)]
    for finder, name, ispkg in sorted(pkgutil.iter_modules(search_paths), key=lambda item: item[1]):
        if not ispkg or name.startswith("_") or name in _NON_PLUGIN_SUBPACKAGES:
            continue
        mod_name = f"moex_spread_lab.strategies.{name}.plugin"
        try:
            if extra_paths and not (strategies_dir / name).is_dir():
                plugin_file = None
                for extra_dir in extra_paths:
                    cand = Path(extra_dir) / name / "plugin.py"
                    if cand.is_file():
                        plugin_file = cand
                        break
                if plugin_file is None:
                    continue
                spec = importlib.util.spec_from_file_location(f"_ext_strategy_{name}_plugin", plugin_file)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            else:
                mod = importlib.import_module(mod_name)
        except ModuleNotFoundError as exc:
            if exc.name == mod_name or exc.name == f"{name}.plugin":
                continue
            raise
        plugin = getattr(mod, "PLUGIN", None)
        if isinstance(plugin, StrategyPlugin):
            discovered[plugin.strategy_id] = plugin

    for ep in sorted(
        importlib.metadata.entry_points(group="moex_spread_lab.strategies"),
        key=lambda item: item.name,
    ):
        if ep.name in discovered:
            continue
        loaded = ep.load()
        if isinstance(loaded, StrategyPlugin):
            discovered[loaded.strategy_id] = loaded

    return StrategyRegistry(discovered)


__all__ = [
    "CALENDAR_EXIT_REASONS",
    "CALENDAR_FILLS",
    "CompatibilityVerdict",
    "ENTRY_BAND_NOTE_RU",
    "EntryBandPoint",
    "InstrumentContext",
    "NON_PRICE_SIGNAL_NOTE_RU",
    "PRICE_FILLS",
    "ParameterSpec",
    "REFERENCE_WARNING",
    "StrategyIntentRun",
    "StrategyPlugin",
    "StrategyRegistry",
    "WindowSnapshot",
    "ZSCORE_CALENDAR_OPTIONS",
    "calendar_price",
    "calendar_row_buffers",
    "discover_strategies",
    "elapsed_calendar_days",
    "make_calendar_trade",
    "population_rolling_stats",
    "row_ts_days",
    "validate_parameter_map",
]
