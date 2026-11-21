"""Backward-compatible shim for ``moex_spread_lab.data.iss.catalog``."""

from moex_spread_lab.data.iss.catalog import *  # noqa: F401,F403
from moex_spread_lab.data.iss.catalog import (  # noqa: F401
    _HISTORY_JSON_CACHE,
    _history_url,
)
