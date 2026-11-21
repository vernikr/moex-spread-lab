"""Compatibility alias for the extracted proof runtime module."""

import sys
from moex_spread_lab.proof_runtime import proof_search as _module

sys.modules[__name__] = _module
