"""Compatibility alias for ``moex_spread_lab.proof.proof_search``."""

import sys

from moex_spread_lab.proof import proof_search as _module

sys.modules[__name__] = _module
