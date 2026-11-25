import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.strategies.pair_screener.screener import screen_pairs
from strategy_fixtures import write_lqdt_lineage, write_near_far_contracts


class ScreenerTests(unittest.TestCase):
    def test_pair_screen_runs_fixed_control_and_risk_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candles = root / "candles"
            candles.mkdir()
            contracts = root / "contracts.json"
            write_near_far_contracts(
                contracts,
                near={"lot_size": 1, "tick_size": 0.5, "tick_value": 5},
                far={"lot_size": 1, "tick_size": 0.05, "tick_value": 0.5},
                schema_version="iss-intake.v1",
            )
            dates = [f"2026-03-{day:02d}" for day in range(1, 26)]
            for secid, offset in (("NEAR", 0.0), ("FAR", 0.5)):
                (candles / f"{secid}_1d.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "iss-candles.v1",
                            "secid": secid,
                            "interval": 24,
                            "rows": [
                                {
                                    "begin": f"{date} 00:00:00",
                                    "close": float(index) + offset,
                                    "open": float(index) + offset,
                                    "high": float(index) + offset,
                                    "low": float(index) + offset,
                                    "price_proxy_kind": "candle_close",
                                }
                                for index, date in enumerate(dates, start=1)
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
            benchmark = root / "benchmark.json"
            write_lqdt_lineage(benchmark, dict.fromkeys(dates, 1.0))

            result = screen_pairs(
                candles,
                benchmark,
                contracts,
                pairs=({"pair_id": "NEAR/FAR", "near": "NEAR", "far": "FAR", "family": "test"},),
            )

        self.assertEqual(result["summary"]["candidate_count"], 1)
        self.assertEqual(result["candidates"][0]["common_rows"], 25)
        self.assertTrue(result["candidates"][0]["stress_screen_passed"])
        self.assertIn("final_X_rub", result["candidates"][0]["h1_control"])


if __name__ == "__main__":
    unittest.main()
