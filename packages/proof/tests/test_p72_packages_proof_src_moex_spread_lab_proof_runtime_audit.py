"""Regression coverage for strict typing of ``proof_runtime.audit`` (Issue #73)."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from typing import Any

from moex_spread_lab.core import universe
from moex_spread_lab.data.lake.data_lake import BarSeries
from moex_spread_lab.proof import audit
from moex_spread_lab.proof.audit import (
    SLIPPAGE_GRID,
    _candles,
    audit_pairs,
    independent_gross,
)
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs


def _candle_payload(
    secid: str,
    closes: list[float],
    *,
    volume: int = 500,
    bad_close_day: int | None = None,
    bad_open_day: int | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for day, close in enumerate(closes, start=1):
        open_px = close - 0.05 * (day % 3)
        high_px = close + 1.0
        low_px = close - 1.0
        if day == bad_close_day:
            open_px = close - 0.8
            high_px = close - 0.5
        if day == bad_open_day:
            open_px = low_px - 0.5
        rows.append(
            {
                "begin": f"2026-03-{day:02d} 00:00:00",
                "close": close,
                "open": open_px,
                "high": high_px,
                "low": low_px,
                "volume": volume,
                "price_proxy_kind": "candle_close",
            }
        )
    return {
        "schema_version": "iss-candles.v1",
        "secid": secid,
        "interval": 24,
        "rows": rows,
    }


def _write_contracts(root: Path, secids: tuple[str, ...]) -> None:
    instruments = [
        {
            "secid": secid,
            "stable_id": secid,
            "board": "RFUD",
            "expiry": "2100-01-01",
            "lot_size": 1000,
            "tick_size": 0.001,
            "tick_value": 1.0,
            "initial_margin": 1000.0,
            "source_vintage": "current_snapshot",
        }
        for secid in secids
    ]
    (root / "data/w1/current-intake.json").write_text(
        json.dumps({"schema_version": "iss-intake.v1", "instruments": instruments}),
        encoding="utf-8",
    )


class AuditStrictRegressionTests(unittest.TestCase):
    def test_module_reexports_and_constants_preserved(self) -> None:
        self.assertIs(audit.PAIRS, universe.PAIRS)
        self.assertEqual(SLIPPAGE_GRID, (0.0, 1.0, 2.0))

    def test_candles_filters_non_string_timestamps(self) -> None:
        series = BarSeries(
            schema_version="iss-candles.v1",
            secid="CNYRUBF",
            interval=24,
            timeframe="1d",
            rows=(
                {"begin": "2026-03-01 00:00:00", "close": 10.0},
                {"begin": None, "close": 11.0},
                {"close": 12.0},
            ),
            source_path="memory",
            source_sha256="0" * 64,
            backend="memory",
            cache_key=("memory",),
            metadata={},
        )
        mapped = _candles(series)
        self.assertEqual(list(mapped), ["2026-03-01 00:00:00"])
        self.assertEqual(mapped["2026-03-01 00:00:00"]["close"], 10.0)

    def test_independent_gross_options_and_open_position_at_end(self) -> None:
        near: dict[str, dict[str, float]] = {}
        far: dict[str, dict[str, float]] = {}
        for day in range(1, 10):
            stamp = f"2026-03-{day:02d} 00:00:00"
            base = 100.0 + (0.1 if day % 2 else -0.1)
            spike = 5.0 if day == 7 else 0.0
            near[stamp] = {"close": base + spike, "open": base + spike}
            far[stamp] = {"close": 100.0, "open": 100.0}
        gross, trades = independent_gross(
            near,
            far,
            1.0,
            1.0,
            lookback=5,
            entry_z=1.5,
            exit_z=0.5,
            delay=1,
            fill="close",
        )
        self.assertEqual(trades, 1)
        self.assertAlmostEqual(gross, -0.2)

        near_hold = dict(near)
        near_hold["2026-03-08 00:00:00"] = {"close": 104.0, "open": 104.0}
        near_hold["2026-03-09 00:00:00"] = {"close": 103.0, "open": 103.0}
        gross_hold, trades_hold = independent_gross(
            near_hold,
            far,
            1.0,
            1.0,
            lookback=5,
            entry_z=1.5,
            exit_z=0.1,
            delay=1,
            fill="open",
        )
        self.assertEqual(trades_hold, 1)
        self.assertAlmostEqual(gross_hold, 1.0)

    def test_independent_gross_rejects_unknown_keyword(self) -> None:
        rows = {"2026-03-01 00:00:00": {"close": 10.0, "open": 10.0}}
        with self.assertRaisesRegex(TypeError, "unexpected keyword argument"):
            independent_gross(rows, rows, 1.0, 1.0, unknown_flag=True)

    def test_audit_pairs_records_blocked_status_and_range_diagnostics(self) -> None:
        closes = [100.0 + 3.0 * math.sin(i) for i in range(30)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data/w1/candles").mkdir(parents=True)
            (root / "data/benchmark").mkdir(parents=True)
            (root / "data/w1/candles/CNYRUBF_1d.json").write_text(
                json.dumps(
                    _candle_payload(
                        "CNYRUBF",
                        closes,
                        volume=10,
                        bad_close_day=2,
                        bad_open_day=3,
                    )
                ),
                encoding="utf-8",
            )
            (root / "data/w1/candles/CRZ6_1d.json").write_text(
                json.dumps(_candle_payload("CRZ6", [c + 2.0 for c in closes], volume=500)),
                encoding="utf-8",
            )
            _write_contracts(root, ("CNYRUBF", "CRZ6"))
            (root / "data/benchmark/lqdt-lineage.json").write_text(
                json.dumps({"schema_version": "invalid-schema", "rows": []}),
                encoding="utf-8",
            )
            result = audit_pairs(ScreenerInputs(root))
        self.assertEqual(result["schema_version"], "pair-audit.v2")
        self.assertEqual(result["summary"]["audited"], 0)
        self.assertEqual(len(result["pairs"]), 1)
        blocked = result["pairs"][0]
        self.assertEqual(blocked["pair"], "CNYRUBF/CRZ6")
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("lqdt-lineage.v1", blocked["reason"])
        self.assertFalse(blocked["tradable_every_day"])
        self.assertEqual(blocked["close_outside_range"], 1)
        self.assertEqual(blocked["open_outside_range"], 1)


if __name__ == "__main__":
    unittest.main()
