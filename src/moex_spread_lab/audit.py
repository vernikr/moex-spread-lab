"""Compatibility alias for ``moex_spread_lab.proof.audit``."""

import sys

from moex_spread_lab.proof import audit as _module

sys.modules[__name__] = _module
