"""Lazy, invalidatable strategy discovery shared by the proof kernel and the web UI.

Discovery used to run once at import time, so a long-lived process kept a stale registry
and every test had to monkey-patch module globals. These accessors cache the derived
values (registry, proof-adapter ids, tunable axes) per registry, can be invalidated, and
accept an explicit ``registry`` so callers inject a registry instead of patching.
"""

from __future__ import annotations

from functools import lru_cache

from moex_spread_lab.core.contracts import plugin_is_builtin
from moex_spread_lab.strategies import (
    StrategyRegistry,
    clear_discovery_cache,
    discover_strategies,
)
from moex_spread_lab.strategies.base import (
    calendar_proof_plugin_ids,
    carry_proof_plugin_ids,
    daily_pair_proof_plugin_ids,
)

from .landscape import Axis

Derived = tuple[StrategyRegistry, frozenset[str], dict[str, dict[str, Axis]]]

_registry: StrategyRegistry | None = None


@lru_cache(maxsize=8)
def _derive(registry: StrategyRegistry) -> Derived:
    """Built-in engines plus explicitly declared, narrowly scoped intent adapters.

    Discovery alone must never route an unimplemented plugin into another engine.
    """
    adapter_ids = (
        frozenset(plugin.strategy_id for plugin in registry if plugin_is_builtin(plugin))
        | calendar_proof_plugin_ids(registry)
        | daily_pair_proof_plugin_ids(registry)
        | carry_proof_plugin_ids(registry)
    )
    axes = {
        plugin.strategy_id: {
            spec.name: Axis(
                int(spec.min_value) if spec.kind == "int" else spec.min_value,
                int(spec.max_value) if spec.kind == "int" else spec.max_value,
                int(spec.step) if spec.kind == "int" else spec.step,
                kind=spec.kind,
            )
            for spec in plugin.parameter_schema()
            if spec.tunable
        }
        for plugin in registry
        if plugin.strategy_id in adapter_ids
    }
    return registry, adapter_ids, axes


def clear_strategy_registry_cache() -> None:
    """Drop cached discovery so the next accessor sees a newly installed plugin."""
    global _registry
    _registry = None
    _derive.cache_clear()
    clear_discovery_cache()


def get_strategy_registry(*, refresh: bool = False) -> StrategyRegistry:
    """Return the process-wide registry, discovering it on first use."""
    global _registry
    if refresh:
        clear_strategy_registry_cache()
    if _registry is None:
        _registry = discover_strategies()
    return _registry


def resolve_strategy_registry(registry: StrategyRegistry | None = None) -> StrategyRegistry:
    """The explicitly injected registry, or the process-wide one when none is supplied."""
    return get_strategy_registry() if registry is None else registry


def get_proof_adapter_ids(registry: StrategyRegistry | None = None) -> frozenset[str]:
    """Ids the proof engines may replay: built-ins plus the validated intent adapters."""
    return _derive(get_strategy_registry() if registry is None else registry)[1]


def get_strategy_axes(registry: StrategyRegistry | None = None) -> dict[str, dict[str, Axis]]:
    """Tunable search domain per proof-enabled strategy, in registry order."""
    return _derive(get_strategy_registry() if registry is None else registry)[2]
