"""Shared cohort-search harness and independent replay checks for test_proof_batch."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.proof_runtime.service import ProofService
from proof_fixtures import fixture

P = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6"}
C = {"universe": "calendar_native", "asset": "SR", "pair": "SRH3/SRM3"}
ACTIVE = {
    **P,
    "strategy": "mean_reversion",
    "parameters": {"lookback": 10, "entry_z": 1.5, "exit_z": 0.5},
}


class CohortCase(unittest.TestCase):
    """Temporary workspace with fixture histories and a seeded proof service."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        fixture(self.root)
        # A fixed seed keeps repeated searches comparable; seed 0 would draw a
        # new one per run and these tests compare two searches against each other.
        save_settings(self.root, {"random_seed": 41})
        self.service = ProofService(self.root)

    def search(self, base=ACTIVE, **kw):
        return research_run(self.service, {**base, **kw}, optimize=True)

    def assert_member_replay(self, row, key, strategy, params):
        """The selected member replays identically and matches manual wealth math."""
        expected = {
            key: params["lookback"],
            "entry_z": params["entry_z"],
            "exit_z": params["exit_z"],
        }
        self.assertEqual(row["selection"]["parameters"], expected)
        self.assertEqual(row["selection"]["strategy"], strategy)
        req = row["selection"]
        rerun = research_run(self.service, req, optimize=False)
        self.assertEqual(rerun["run_id"], row["run_id"])
        self.assertEqual(rerun["evaluation"], row["evaluation"])
        self.assertAlmostEqual(
            row["training_annualized_excess_pp"], self._manual_training_excess(row, req)
        )

    def _manual_training_excess(self, row, req):
        """Independent ordinary replay cut on this member's own training end."""
        cutoff = row["evaluation"]["periods"]["training"][-1]
        prefix = self.service.prepare(req)(req, through=cutoff)
        dates = row["evaluation"]["periods"]["training"]
        prices = {
            r["date"]: r["price"]
            for r in json.loads((self.root / "data/benchmark/lqdt-lineage.json").read_text())[
                "rows"
            ]
        }
        wealth = 30_000.0
        last_px = prices[dates[0]]
        previous_net = 0.0
        reserved = 0
        for r in prefix["ledger"]:
            d = r["date"]
            price = prices[d]
            net = r["cumulative_net_rub"]
            wealth += (wealth - reserved) * (price / last_px - 1) + net - previous_net
            reserved = sum(
                t["margin_rub"]
                for t in prefix["trades"]
                if t["entry"]["date"] <= d < t["exit"]["date"]
            )
            last_px = price
            previous_net = net
        years = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days / 365
        return 100 * (
            (wealth / 30_000) ** (1 / years) - (last_px / prices[dates[0]]) ** (1 / years)
        )
