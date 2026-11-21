"""Backward-compatible shim for ``moex_spread_lab.accounting.run_artifacts``."""

from moex_spread_lab.accounting.run_artifacts import *  # noqa: F401,F403
from moex_spread_lab.accounting.run_artifacts import (  # noqa: F401
    _atomic_write_json,
    _validate_report,
)
