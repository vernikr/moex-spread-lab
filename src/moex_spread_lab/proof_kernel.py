"""Compatibility alias for ``moex_spread_lab.proof.proof_kernel``."""

import sys

from moex_spread_lab.proof import proof_kernel as _module

sys.modules[__name__] = _module
