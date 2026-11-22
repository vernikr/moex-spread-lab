"""Native and H1 calendar spread Z-score mean-reversion strategy subpackage."""

from .engine import compute_calendar_zscore_entry_bands, zscore_trades
from .plugin import PLUGIN, CalendarZscorePlugin

__all__ = [
    "PLUGIN",
    "CalendarZscorePlugin",
    "compute_calendar_zscore_entry_bands",
    "zscore_trades",
]
