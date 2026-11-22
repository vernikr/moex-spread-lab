"""Backward-compatible shim for ``moex_spread_lab.accounting.run_artifact_import``."""

import sys as _sys

import moex_spread_lab.accounting.run_artifact_import as _mod

_sys.modules[__name__] = _mod
