import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.carry import build_carry_study


def source(secid, rows):
    return {
        "schema_version": "iss-history.v1",
        "secid": secid,
        "rows": rows,
        "source": {"raw_response_sha256": [f"{secid}-hash"]},
    }


class CarryTests(unittest.TestCase):
    def test_funding_and_basis_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            contracts = Path(directory) / "contracts.json"
            contracts.write_text(json.dumps({"instruments": [
                {"secid": "NEAR", "stable_id": "n", "board": "RFUD", "expiry": "2100-01-01", "lot_size": 1000, "tick_size": 0.001, "tick_value": 1, "initial_margin": 900, "source_vintage": "current_snapshot"},
                {"secid": "FAR", "stable_id": "f", "board": "RFUD", "expiry": "2026-12-17", "lot_size": 1000, "tick_size": 0.001, "tick_value": 1, "initial_margin": 1000, "source_vintage": "current_snapshot"},
            ]}), encoding="utf-8")
            result = build_carry_study(
                source("NEAR", [
                    {"TRADEDATE": "2026-01-01", "SETTLEPRICE": 10.0, "SWAPRATE": 0.004},
                    {"TRADEDATE": "2026-01-02", "SETTLEPRICE": 10.1, "SWAPRATE": 0.005},
                ]),
                source("FAR", [
                    {"TRADEDATE": "2026-01-01", "SETTLEPRICE": 10.2, "SWAPRATE": 0.0},
                    {"TRADEDATE": "2026-01-02", "SETTLEPRICE": 10.25, "SWAPRATE": 0.0},
                ]),
                contracts,
                near="NEAR",
                far="FAR",
            )

        self.assertEqual(result["summary"]["total_basis_pnl"], -50.0)
        self.assertEqual(result["summary"]["total_funding_received"], 5.0)
        self.assertEqual(result["summary"]["total_gross_carry_pnl"], -45.0)
        self.assertFalse(result["summary"]["transaction_costs_included"])


if __name__ == "__main__":
    unittest.main()
