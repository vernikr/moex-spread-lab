"""Backward-compatible shim for ``moex_spread_lab.data.retention.data_archive_candidates``."""

import sys as _sys

import moex_spread_lab.data.retention.data_archive_candidates as _mod

_sys.modules[__name__] = _mod
