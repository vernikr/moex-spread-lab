"""Tests for offline W1-W7 research waves (relocated from core under F7 / #14)."""

import unittest
from pathlib import Path

from moex_spread_lab.accounting.research import run_research_pipeline

ROOT = Path(__file__).resolve().parents[3]


class ResearchWavesTests(unittest.TestCase):
    def test_research_waves_pass_fixture_gates_but_remain_inconclusive(self):
        report = run_research_pipeline(
            ROOT / "config/run-manifest.json", ROOT / "fixtures/research/offline.json"
        )
        self.assertTrue(report["all_fixture_gates_passed"])
        self.assertEqual(report["release_state"], "inconclusive")
        self.assertEqual(set(report["waves"]), {"W1", "W2", "W3", "W4", "W5", "W6", "W7"})

    def test_execution_replays_stale_and_partial_paths(self):
        report = run_research_pipeline(
            ROOT / "config/run-manifest.json", ROOT / "fixtures/research/offline.json"
        )
        self.assertEqual(report["waves"]["W4"]["outcome_counts"]["rejected_stale"], 1)
        self.assertEqual(report["waves"]["W4"]["outcome_counts"]["partial_unbalanced"], 1)

    def test_matched_ledger_starts_at_zero_and_has_no_top_up(self):
        report = run_research_pipeline(
            ROOT / "config/run-manifest.json", ROOT / "fixtures/research/offline.json"
        )
        portfolio = report["waves"]["W5"]
        self.assertEqual(portfolio["rows"][0]["X"], 0)
        self.assertEqual(portfolio["lifetime_topup_usage"], 0)
        self.assertEqual(portfolio["execution_attempts_recorded"], 4)
        self.assertEqual(portfolio["unbalanced_attempts_preserved"], 1)


if __name__ == "__main__":
    unittest.main()
