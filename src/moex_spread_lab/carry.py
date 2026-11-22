"""Backward-compatible shim for ``moex_spread_lab.strategies.funding_carry.carry``."""

from moex_spread_lab.strategies.funding_carry.carry import *  # noqa: F403
from moex_spread_lab.strategies.funding_carry.carry import (  # noqa: F401
    _history_url,
    _transaction_cost_profile,
)
