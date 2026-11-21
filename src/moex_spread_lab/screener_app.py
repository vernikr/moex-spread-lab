"""Backward-compatible umbrella alias to ``moex_spread_lab.web.server``."""

from __future__ import annotations

import importlib
import sys

_module = importlib.import_module("moex_spread_lab.web.server")
sys.modules[__name__] = _module
