"""Backward-compatible shim for ``moex_spread_lab.data.lake.data_lake``."""

from moex_spread_lab.data.lake.data_lake import *  # noqa: F401,F403
from moex_spread_lab.data.lake.data_lake import (  # noqa: F401
    _atomic_write_json,
    _read_legacy_series,
)
