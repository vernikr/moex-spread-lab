"""Compatibility alias for ``moex_spread_lab.proof_runtime.proof_pool``."""

import sys
from moex_spread_lab.proof_runtime import proof_pool as _module

sys.modules[__name__] = _module
