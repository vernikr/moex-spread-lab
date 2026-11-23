"""MOEX Spread Lab Layer 2 strategies: plugin discovery registry and isolated strategy subpackages."""

from moex_spread_lab.core.contracts import (
    CompatibilityVerdict,
    EntryBandPoint,
    InstrumentContext,
    ParameterSpec,
    StrategyIntentRun,
    StrategyPlugin,
    WindowSnapshot,
)

from .base import (
    PRICE_FILLS,
    REFERENCE_WARNING,
    StrategyRegistry,
    discover_strategies,
)
from .registry import clear_discovery_cache
from .signals import attach_entry_levels, pair_fill

__all__ = [
    "PRICE_FILLS",
    "REFERENCE_WARNING",
    "CompatibilityVerdict",
    "EntryBandPoint",
    "InstrumentContext",
    "ParameterSpec",
    "StrategyIntentRun",
    "StrategyPlugin",
    "StrategyRegistry",
    "WindowSnapshot",
    "attach_entry_levels",
    "clear_discovery_cache",
    "discover_strategies",
    "pair_fill",
]
