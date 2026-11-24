"""Canonical MOEX instrument, pair, and strategy universe registry (Layer 1).

Concrete catalogs live in the sibling modules; the generic universe identifiers
are re-exported from ``moex_spread_lab.core.universe`` so callers can use one
import site. ``moex_spread_lab.core.universe`` keeps a deprecation accessor for
the Layer-1 names during the migration window.
"""

from __future__ import annotations

from moex_spread_lab.core.universe import CALENDAR_UNIVERSE, PAIR_UNIVERSE

from .assets import (
    ASSETS,
    CROSS_EXPIRY_PAIRS,
    FX_SOURCE,
    MONTH_CODES,
    QUARTER_CODES,
    SYNTHETIC_TRIANGLE_PAIRS,
    contract_chain,
)
from .pairs import (
    BASE_PAIRS,
    EXTRA_PAIRS,
    GLD_HISTORICAL_FAR,
    GLD_HISTORICAL_PAIRS,
    HISTORICAL_FAR_BY_NEAR,
    HISTORICAL_PERPETUAL_PAIRS,
    HISTORY_WINDOWS,
    PAIRS,
    PERPETUAL_HISTORY_STARTS,
    PERPETUAL_HISTORY_TILL,
    SCREEN_PAIRS,
)

__all__ = [
    "ASSETS",
    "BASE_PAIRS",
    "CALENDAR_UNIVERSE",
    "CROSS_EXPIRY_PAIRS",
    "EXTRA_PAIRS",
    "FX_SOURCE",
    "GLD_HISTORICAL_FAR",
    "GLD_HISTORICAL_PAIRS",
    "HISTORICAL_FAR_BY_NEAR",
    "HISTORICAL_PERPETUAL_PAIRS",
    "HISTORY_WINDOWS",
    "MONTH_CODES",
    "PAIRS",
    "PAIR_UNIVERSE",
    "PERPETUAL_HISTORY_STARTS",
    "PERPETUAL_HISTORY_TILL",
    "QUARTER_CODES",
    "SCREEN_PAIRS",
    "SYNTHETIC_TRIANGLE_PAIRS",
    "contract_chain",
]
