"""Backward-compatible shim for ``moex_spread_lab.core.w0``."""

from moex_spread_lab.core.w0 import *  # noqa: F403
from moex_spread_lab.core.w0 import (  # noqa: F401
    _HASHED_JSON_CACHE,
    _HASHED_JSON_CACHE_CAPACITY,
    _HashedJSONCache,
    _require,
    _resolve,
    _run_id,
    _validate_contract_number,
)
