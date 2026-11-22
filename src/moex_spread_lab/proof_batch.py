"""Compatibility alias for ``moex_spread_lab.proof_runtime.proof_batch``."""

import sys

from moex_spread_lab.proof_runtime import proof_batch as _module

sys.modules[__name__] = _module
