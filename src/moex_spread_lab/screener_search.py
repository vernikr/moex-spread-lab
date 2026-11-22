"""Compatibility alias for ``moex_spread_lab.proof_runtime.screener_search``."""

import sys

from moex_spread_lab.proof_runtime import screener_search as _module

sys.modules[__name__] = _module
