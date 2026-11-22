"""Compatibility alias for ``moex_spread_lab.proof_runtime.audit``."""

import sys

from moex_spread_lab.proof_runtime import audit as _module

sys.modules[__name__] = _module
