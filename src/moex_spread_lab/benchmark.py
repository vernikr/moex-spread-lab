"""Backward-compatible shim for ``moex_spread_lab.data.benchmark.lineage``."""

import sys as _sys

import moex_spread_lab.data.benchmark.lineage as _mod

_sys.modules[__name__] = _mod
