import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.strategies.pair_mean_reversion.analytics import run_mean_reversion_baseline
from moex_spread_lab.accounting.ledger import build_ledger
from moex_spread_lab.accounting.validation import build_validation


def make_baseline():
    rows = []
    spreads = [0, 10, 20, 30, 20, 10, 0, -10, -20, -10, 0, 10, 20, 10, 0, -10, -20, -10, 0, 10]
    for index, spread in enumerate(spreads):
        rows.append({
            "date": f"2026-01-{index + 1:02d}",
            "timestamp": f"2026-01-{index + 1:02d} 00:00:00",
            "near_close": 10.0 + spread / 1000,
            "far_close": 10.0,
            "spread_ticks": spread,
            "benchmark_price": 1.0,
            "benchmark_source_kind": "observed",
        })
    return run_mean_reversion_baseline({
        "near": "NEAR",
        "far": "FAR",
        "timeframe": "1d",
        "contracts": {
            "NEAR": {"lot_size": 1000, "tick_size": 0.001, "tick_value": 1.0, "initial_margin": 900},
            "FAR": {"lot_size": 1000, "tick_size": 0.001, "tick_value": 1.0, "initial_margin": 1000},
        },
        "rows": rows,
        "provenance": {},
    }, lookback=3, entry_z=1.0, exit_z=0.5)


class ValidationTests(unittest.TestCase):
    def test_validation_has_split_null_and_blockers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline_path = root / "baseline.json"
            baseline_path.write_text(json.dumps(make_baseline()), encoding="utf-8")
            ledger_path = root / "ledger.json"
            ledger_path.write_text(json.dumps(build_ledger(baseline_path)), encoding="utf-8")
            report = build_validation(baseline_path, ledger_path)

        self.assertEqual(report["verdict"], "inconclusive_no_pass")
        self.assertEqual(report["splits"]["train"]["row_count"], 12)
        self.assertEqual(report["splits"]["sealed_confirmation"]["row_count"], 4)
        self.assertTrue(report["gates"]["sign_flip_diagnostic"])
        self.assertGreaterEqual(len(report["conclusion_blockers"]), 3)
        self.assertTrue(report["no_profitability_claim"])


if __name__ == "__main__":
    unittest.main()
