"""Backward-compatible shim for ``moex_spread_lab.strategies.calendar.calendar_spreads``."""

from moex_spread_lab.strategies.calendar.calendar_spreads import *  # noqa: F401,F403
from moex_spread_lab.strategies.calendar.calendar_spreads import (  # noqa: F401
    _positive_spread_volume_rows,
    _stress_ok,
)
