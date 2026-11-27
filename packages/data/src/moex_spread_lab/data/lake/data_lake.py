"""Backward-compatible lake facade for the external ``strategies`` callers.

Only the four names the strategies subpackages still import are re-exported;
canonical imports come from ``moex_spread_lab.data.lake`` submodules or the
public ``moex_spread_lab.data`` exports. The facade is retired once external
callers switch in Post-Wave 2 (#188 follow-up).
"""

from __future__ import annotations

from .bar_types import BarSeries, DataLakeError
from .legacy_series import LegacyJsonBarStore
from .market_store import MarketDataStore

__all__ = ["BarSeries", "DataLakeError", "LegacyJsonBarStore", "MarketDataStore"]
