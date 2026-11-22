"""MOEX Spread Lab Layer 2 strategies: plugin discovery registry and isolated strategy subpackages."""

from .base import (
    PRICE_FILLS,
    REFERENCE_WARNING,
    CompatibilityVerdict,
    EntryBandPoint,
    InstrumentContext,
    ParameterSpec,
    StrategyIntentRun,
    StrategyPlugin,
    StrategyRegistry,
    WindowSnapshot,
    discover_strategies,
)
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
    "discover_strategies",
    "pair_fill",
]
