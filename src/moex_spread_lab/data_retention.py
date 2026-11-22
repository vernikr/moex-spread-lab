"""Backward-compatible shim for ``moex_spread_lab.data.retention.data_retention``."""

from moex_spread_lab.data.retention.data_retention import *  # noqa: F403
from moex_spread_lab.data.retention.data_retention import (  # noqa: F401
    _prune_empty_parents,
    _safe_data_path,
    _validate_archive_path,
)
