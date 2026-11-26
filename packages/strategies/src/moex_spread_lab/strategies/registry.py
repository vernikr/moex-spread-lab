"""StrategyRegistry and auto-discovery via ``pkgutil`` + ``importlib.metadata``."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
import pkgutil
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.core import universe as _universe
from moex_spread_lab.core.contracts import StrategyPlugin, plugin_proof_adapter
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE, PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

_STRATEGIES_DIR = Path(__file__).resolve().parent
# Memo entry keyed by strategies-dir listing + current entry_points callable so
# patched discovery sources or added plugin folders never hit a stale cache.
_DISCOVERY_CACHE: dict[str, object] = {}
_PRICE_FILL_KEYS = frozenset({"next_open", "next_close", "reference_same_bar"})
_NON_PLUGIN_SUBPACKAGES = frozenset({"calendar", "pair_screener"})
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

    def __iter__(self) -> Iterator[StrategyPlugin]:
        return iter(self._plugins.values())

    def __len__(self) -> int:
        return len(self._plugins)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._plugins.keys())

    def values(self) -> tuple[StrategyPlugin, ...]:
        return tuple(self._plugins.values())

    def get(self, strategy_id: str) -> StrategyPlugin:
        try:
            return self._plugins[strategy_id]
        except KeyError as exc:
            raise W0Error(f"Неизвестная стратегия: {strategy_id}") from exc

    def as_dict(self) -> dict[str, StrategyPlugin]:
        return dict(self._plugins)


def _family_counts(plugins: tuple[StrategyPlugin, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for plugin in plugins:
        counts[plugin.family_id] = counts.get(plugin.family_id, 0) + 1
    return counts


def _has_price_fills(plugin: StrategyPlugin) -> bool:
    return (
        len(plugin.supported_fills) == len(_PRICE_FILL_KEYS)
        and frozenset(plugin.supported_fills) == _PRICE_FILL_KEYS
    )


def calendar_proof_plugin_ids(registry: StrategyRegistry) -> frozenset[str]:
    """Opt in only unambiguous calendar price-intent plugins to generic proof."""
    plugins = registry.values()
    counts = _family_counts(plugins)
    return frozenset(
        p.strategy_id
        for p in plugins
        if plugin_proof_adapter(p) == "calendar_price_intents.v1"
        and p.supported_universes == frozenset({CALENDAR_UNIVERSE})
        and _has_price_fills(p)
        and counts[p.family_id] == 1
    )


BUILTIN_STRATEGY_IDS: frozenset[str] = frozenset(
    str(key) for key in cast("Mapping[str, Any]", _universe.STRATEGIES)
)


def daily_pair_proof_plugin_ids(registry: StrategyRegistry) -> frozenset[str]:
    """Opt in a distinct pair-only price strategy, never funding carry."""
    plugins = registry.values()
    counts = _family_counts(plugins)
    return frozenset(
        p.strategy_id
        for p in plugins
        if (
            (
                plugin_proof_adapter(p) == "daily_pair_price_intents.v1"
                and p.requires_daily_bars is True
            )
            or (
                plugin_proof_adapter(p) == "pair_price_intents.v1"
                and p.requires_daily_bars is False
            )
        )
        and p.supported_universes == frozenset({PAIR_UNIVERSE})
        and _has_price_fills(p)
        # Third-party plugins opt in only when their family is unambiguous.
        and (p.strategy_id in BUILTIN_STRATEGY_IDS or counts[p.family_id] == 1)
    )


def carry_proof_plugin_ids(registry: StrategyRegistry) -> frozenset[str]:
    """Opt in unique, daily short-near settlement-intent carry families."""
    plugins = registry.values()
    counts = _family_counts(plugins)
    return frozenset(
        p.strategy_id
        for p in plugins
        if plugin_proof_adapter(p) == "carry_settlement_intents.v1"
        and p.supported_universes == frozenset({PAIR_UNIVERSE})
        and p.supported_fills == ("same_day_settlement",)
        and p.requires_daily_bars is True
        and (p.strategy_id in BUILTIN_STRATEGY_IDS or counts[p.family_id] == 1)
        and bool(p.parameter_schema())
        and all(spec.tunable for spec in p.parameter_schema())
    )


def _register_plugin(discovered: dict[str, StrategyPlugin], plugin: StrategyPlugin) -> None:
    strategy_id: Any = plugin.strategy_id
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise W0Error("Стратегия должна иметь непустой строковый идентификатор.")
    previous = discovered.get(strategy_id)
    if previous is not None and previous is not plugin:
        raise W0Error(f"Повторяющийся идентификатор стратегии: {strategy_id}")
    discovered[strategy_id] = plugin


def _load_external_plugin_module(name: str, extra_paths: tuple[Path, ...]) -> object | None:
    plugin_file = next(
        (
            cand
            for extra_dir in extra_paths
            if (cand := Path(extra_dir) / name / "plugin.py").is_file()
        ),
        None,
    )
    if plugin_file is None:
        return None
    spec = importlib.util.spec_from_file_location(f"_ext_strategy_{name}_plugin", plugin_file)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_subpackage_plugin(
    name: str, strategies_dir: Path, extra_paths: tuple[Path, ...]
) -> StrategyPlugin | None:
    mod_name = f"moex_spread_lab.strategies.{name}.plugin"
    try:
        if extra_paths and not (strategies_dir / name).is_dir():
            mod = _load_external_plugin_module(name, extra_paths)
        else:
            mod = importlib.import_module(mod_name)
    except ModuleNotFoundError as exc:
        if exc.name in {mod_name, f"{name}.plugin"}:
            return None
        raise
    plugin = getattr(mod, "PLUGIN", None) if mod is not None else None
    return plugin if isinstance(plugin, StrategyPlugin) else None


def _strategies_dir_signature(strategies_dir: Path) -> tuple[str, ...] | None:
    try:
        with os.scandir(strategies_dir) as entries:
            return tuple(sorted(entry.name for entry in entries))
    except OSError:
        return None


def discover_strategies(*, extra_paths: tuple[Path, ...] = ()) -> StrategyRegistry:
    """Auto-discover strategy plugins via ``pkgutil.iter_modules`` and entry points.

    Memoized per process by directory signature and ``entry_points`` identity; callers
    adding entry points at runtime call :func:`clear_discovery_cache` first.
    """
    if extra_paths:
        return _discover_uncached(extra_paths)
    signature = _strategies_dir_signature(_STRATEGIES_DIR)
    if signature is None:
        return _discover_uncached(())
    key = (signature, importlib.metadata.entry_points)
    cached = _DISCOVERY_CACHE.get("registry")
    if _DISCOVERY_CACHE.get("key") != key or not isinstance(cached, StrategyRegistry):
        cached = _discover_uncached(())
        _DISCOVERY_CACHE["registry"] = cached
        _DISCOVERY_CACHE["key"] = key
    return cached


def clear_discovery_cache() -> None:
    """Drop memoized discovery so the next call re-scans folders and entry points."""
    _DISCOVERY_CACHE.clear()


def _discover_uncached(extra_paths: tuple[Path, ...]) -> StrategyRegistry:
    strategies_dir = _STRATEGIES_DIR
    discovered: dict[str, StrategyPlugin] = {}
    search_paths = [str(strategies_dir), *(str(p) for p in extra_paths)]
    for _finder, name, ispkg in sorted(
        pkgutil.iter_modules(search_paths), key=lambda item: item[1]
    ):
        if not ispkg or name.startswith("_") or name in _NON_PLUGIN_SUBPACKAGES:
            continue
        plugin = _load_subpackage_plugin(name, strategies_dir, extra_paths)
        if plugin is not None:
            _register_plugin(discovered, plugin)

    for ep in sorted(
        importlib.metadata.entry_points(group="moex_spread_lab.strategies"),
        key=lambda item: item.name,
    ):
        loaded = ep.load()
        if not isinstance(loaded, StrategyPlugin):
            raise W0Error(f"Точка входа стратегии {ep.name} не реализует StrategyPlugin.")
        _register_plugin(discovered, loaded)

    return StrategyRegistry(discovered)
