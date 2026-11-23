"""Shared request fixtures and service scaffolding for the proof-window tests.

Extracted from ``test_proof_windows.py`` by P7.2 test-health leaf #132 so the
test module stays inside the file-size and cognitive-complexity limits. The
requests and the fill matrix are shared verbatim; no expectation lives here.
"""

import tempfile
import unittest
from pathlib import Path
from typing import Any

from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.proof_runtime.service import ProofService
from proof_fixtures import two_calendar_windows

PAIR = {
    "universe": "pair_screen",
    "pair": "CNYRUBF/CRZ6",
    "strategy": "mean_reversion",
    "parameters": {"lookback": 6, "entry_z": 0.8, "exit_z": 0.1},
    "sizing": "max_affordable",
}
CAL = {
    "universe": "calendar_native",
    "asset": "SR",
    "pair": "SRH3/SRM3",
    "strategy": "zscore_mean_reversion",
    "parameters": {"window": 10, "entry_z": 1.5, "exit_z": 0.5},
    "sizing": "max_affordable",
}
FLAT = {**CAL, "pair": "SRM3/SRU3"}
FILLS = ("next_open", "next_close", "reference_same_bar")


def group_request(fill: str) -> dict[str, Any]:
    """Pair request widened to a checked cross-universe group for the given fill."""
    return {
        **PAIR,
        "fill": fill,
        "targets": [
            {k: v for k, v in r.items() if k in ("universe", "asset", "pair")} for r in (PAIR, CAL)
        ],
    }


class FixtureCase(unittest.TestCase):
    """Temporary proof service seeded with the two-calendar-window fixture."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        two_calendar_windows(self.root)
        self.service = ProofService(self.root)

    def run_proof(self, req):
        return research_run(self.service, req, optimize=False, include_plateau=False)
