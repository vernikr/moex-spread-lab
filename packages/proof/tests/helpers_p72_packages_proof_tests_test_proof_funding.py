"""Shared replay fixture for funding tests split from test_proof_funding (P7.2, #129)."""

import tempfile
import unittest
from pathlib import Path

from funding_fixtures import PAIR, fixture
from moex_spread_lab.proof.service import ProofService
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs, _pair, catalog


class FundingReplayCase(unittest.TestCase):
    """Shared ProofService fixture for funding replay scenarios."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.days = fixture(self.root)
        self.service = ProofService(self.root)
        self.inputs = ScreenerInputs(self.root)

    def entry(self, pair=PAIR):
        return catalog(self.inputs, registry=(_pair(pair),))["pairs"][0]
