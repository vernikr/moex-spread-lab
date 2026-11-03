import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.screener import screen_pairs


class ScreenerTests(unittest.TestCase):
    def test_pair_screen_runs_fixed_control_and_risk_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candles = root / "candles"
            candles.mkdir()
            contracts = root / "contracts.json"
            contracts.write_text(json.dumps({"schema_version": "iss-intake.v1", "instruments": [
                {"secid": "NEAR", "stable_id": "n", "board": "RFUD", "expiry": "2100-01-01", "lot_size": 1, "tick_size": 0.5, "tick_value": 5, "initial_margin": 900, "source_vintage": "current_snapshot"},
                {"secid": "FAR", "stable_id": "f", "board": "RFUD", "expiry": "2026-12-17", "lot_size": 1, "tick_size": 0.05, "tick_value": 0.5, "initial_margin": 1000, "source_vintage": "current_snapshot"},
            ]}), encoding="utf-8")
            dates = [f"2026-03-{day:02d}" for day in range(1, 26)]
            for secid, offset in (("NEAR", 0.0), ("FAR", 0.5)):
                (candles / f"{secid}_1d.json").write_text(json.dumps({
                    "schema_version": "iss-candles.v1",
                    "secid": secid,
                    "interval": 24,
                    "rows": [{
                        "begin": f"{date} 00:00:00",
                        "close": float(index) + offset,
                        "open": float(index) + offset,
                        "high": float(index) + offset,
                        "low": float(index) + offset,
                        "price_proxy_kind": "candle_close",
                    } for index, date in enumerate(dates, start=1)],
                }), encoding="utf-8")
            benchmark = root / "benchmark.json"
            benchmark.write_text(json.dumps({
                "schema_version": "lqdt-lineage.v1",
                "rows": [{"date": date, "price": 1.0, "source_kind": "observed", "source_ticker": "LQDT"} for date in dates],
            }), encoding="utf-8")

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
