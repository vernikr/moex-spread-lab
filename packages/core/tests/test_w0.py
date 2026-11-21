import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.core.w0 import (
    W0Error,
    canonical_bytes,
    manifest_config_hash,
    normalize_fixture,
    scan_secrets,
)
from moex_spread_lab.accounting.research import run_research_pipeline


ROOT = Path(__file__).resolve().parents[3]


class W0Tests(unittest.TestCase):
    def test_canonical_hash_is_independent_of_object_order(self):
        self.assertEqual(canonical_bytes({"b": 2, "a": 1}), canonical_bytes({"a": 1, "b": 2}))

    def test_manifest_hash_matches_pinned_manifest(self):
        manifest = json.loads((ROOT / "config/run-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["config_hash"], manifest_config_hash(manifest))

    def test_fixture_normalization_is_sorted(self):
        fixture = json.loads((ROOT / "fixtures/w0/replay.json").read_text(encoding="utf-8"))
        normalized = normalize_fixture(fixture)
        self.assertEqual([r["record_id"] for r in normalized["records"]], ["probe-001", "probe-002"])

    def test_secret_scan_detects_private_key_pattern(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = "-----BEGIN " + "RSA PRIVATE KEY-----\n"
            (root / "sample.txt").write_text(marker, encoding="utf-8")
            self.assertTrue(scan_secrets(root))

    def test_secret_scan_ignores_clean_fixture(self):
        self.assertEqual(scan_secrets(ROOT / "fixtures"), [])

    def test_tracked_secret_scan_uses_git_index(self):
        self.assertEqual(scan_secrets(ROOT, tracked_only=True), [])

    def test_research_waves_pass_fixture_gates_but_remain_inconclusive(self):
        report = run_research_pipeline(ROOT / "config/run-manifest.json", ROOT / "fixtures/research/offline.json")
        self.assertTrue(report["all_fixture_gates_passed"])
        self.assertEqual(report["release_state"], "inconclusive")
        self.assertEqual(set(report["waves"]), {"W1", "W2", "W3", "W4", "W5", "W6", "W7"})

    def test_execution_replays_stale_and_partial_paths(self):
        report = run_research_pipeline(ROOT / "config/run-manifest.json", ROOT / "fixtures/research/offline.json")
        self.assertEqual(report["waves"]["W4"]["outcome_counts"]["rejected_stale"], 1)
        self.assertEqual(report["waves"]["W4"]["outcome_counts"]["partial_unbalanced"], 1)

    def test_matched_ledger_starts_at_zero_and_has_no_top_up(self):
        report = run_research_pipeline(ROOT / "config/run-manifest.json", ROOT / "fixtures/research/offline.json")
        portfolio = report["waves"]["W5"]
        self.assertEqual(portfolio["rows"][0]["X"], 0)
        self.assertEqual(portfolio["lifetime_topup_usage"], 0)
        self.assertEqual(portfolio["execution_attempts_recorded"], 4)
        self.assertEqual(portfolio["unbalanced_attempts_preserved"], 1)


if __name__ == "__main__":
    unittest.main()
