import json
import math
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.proof_runtime.audit import audit_pairs, independent_gross
from moex_spread_lab.web.server import ScreenerInputs


def _rows(secid: str, closes: list[float]) -> dict:
    return {
        "schema_version": "iss-candles.v1",
        "secid": secid,
        "interval": 24,
        "rows": [
            {
                "begin": f"2026-03-{day:02d} 00:00:00",
                "close": close,
                "open": close - 0.05 * (day % 3),
                "high": close + 1,
                "low": close - 1,
                "volume": 500,
                "price_proxy_kind": "candle_close",
            }
            for day, close in enumerate(closes, start=1)
        ],
    }


class AuditTests(unittest.TestCase):
    def test_independent_recomputation_matches_engine_and_flags_overlay(self):
        closes = [100 + 3 * math.sin(i) + (8 if i in (24, 25) else 0) for i in range(30)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data/w1/candles").mkdir(parents=True)
            (root / "data/benchmark").mkdir(parents=True)
            for secid, offset in (("CNYRUBF", 0), ("CRZ6", 2)):
                (root / f"data/w1/candles/{secid}_1d.json").write_text(
                    json.dumps(_rows(secid, [c + offset for c in closes])), encoding="utf-8"
                )
            instruments = [
                {
                    "secid": s,
                    "stable_id": s,
                    "board": "RFUD",
                    "expiry": "2100-01-01",
                    "lot_size": 1000,
                    "tick_size": 0.001,
                    "tick_value": 1.0,
                    "initial_margin": 1000.0,
                    "source_vintage": "current_snapshot",
                }
                for s in ("CNYRUBF", "CRZ6")
            ]
            (root / "data/w1/current-intake.json").write_text(
                json.dumps({"schema_version": "iss-intake.v1", "instruments": instruments}),
                encoding="utf-8",
            )
            (root / "data/benchmark/lqdt-lineage.json").write_text(
                json.dumps(
                    {
                        "schema_version": "lqdt-lineage.v1",
                        "rows": [
                            {
                                "date": f"2026-03-{d:02d}",
                                "price": 1 + 0.001 * d,
                                "source_kind": "observed",
                                "source_ticker": "LQDT",
                            }
                            for d in range(1, 31)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = audit_pairs(ScreenerInputs(root))
        self.assertEqual(result["summary"]["audited"], 1)
        self.assertEqual(result["summary"]["engine_matches_independent"], 1)
        row = result["pairs"][0]
        self.assertEqual(row["weekend_rows"], 9)
        self.assertTrue(row["tradable_every_day"])
        by_slippage = row["final_X_rub_by_slippage"]
        self.assertGreaterEqual(by_slippage["0.0"], by_slippage["1.0"])
        self.assertGreaterEqual(by_slippage["1.0"], by_slippage["2.0"])

    def test_independent_gross_flat_spread_is_zero(self):
        rows = {f"2026-03-{d:02d} 00:00:00": {"close": 10.0, "open": 10.0} for d in range(1, 30)}
        self.assertEqual(independent_gross(rows, rows, 1.0, 1.0, fill="open"), (0.0, 0))


if __name__ == "__main__":
    unittest.main()
