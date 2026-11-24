"""Compatibility alias for ``moex_spread_lab.proof.proof_batch``."""

import sys

from moex_spread_lab.proof import proof_batch as _module

sys.modules[__name__] = _module
