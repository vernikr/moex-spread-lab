"""Backward-compatible shim for ``moex_spread_lab.data.retention.data_inventory``."""

from moex_spread_lab.data.retention.data_inventory import *  # noqa: F401,F403
from moex_spread_lab.data.retention.data_inventory import (  # noqa: F401
    _inspect_file,
    _policy,
)
