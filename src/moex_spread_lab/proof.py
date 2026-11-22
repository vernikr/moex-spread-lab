"""Compatibility alias for ``moex_spread_lab.proof_runtime.service``."""

import sys

from moex_spread_lab.proof_runtime import service as _module

sys.modules[__name__] = _module
