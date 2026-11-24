"""Regression coverage for P7.2 strict typing of proof_runtime/calendar_adapter.py (#75)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.costs import cost_totals
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import run_id
from moex_spread_lab.proof import calendar_adapter
from moex_spread_lab.proof.calendar_adapter import (
    calendar_instrument,
    calendar_proof,
    calendar_provenance,
)
from moex_spread_lab.proof.payload_tools import PROOF_SCHEMA
from moex_spread_lab.strategies import REFERENCE_WARNING


def _write_intake(root: Path) -> None:
    intake = root / "data/w1/current-intake.json"
    intake.parent.mkdir(parents=True, exist_ok=True)
    intake.write_text(
        json.dumps(
            {
                "instruments": [
                    {"secid": "SRH3", "expiry": "2023-03-16"},
                    {"secid": "SRM3", "expiry": "2023-06-15"},
                ]
            }
        ),
        encoding="utf-8",
    )


def _sample_payload(*, fill: str = "next_open", timeframe: str = "1d") -> dict[str, Any]:
    return {
        "schema_version": "calendar-proof.v1",
        "selection": {
            "pair": "SRH3/SRM3",
            "spread_instrument": "SRH3SRM3",
            "strategy": "zscore_mean_reversion",
            "fill": fill,
            "parameters": {"window": 3, "entry_z": 1.5, "exit_z": 0.5},
        },
        "window": {
            "near": "SRH3",
            "far": "SRM3",
            "near_expiry": "2023-03-10",
            "timeframe": timeframe,
            "price_source": "native_daily_spread",
        },
        "contract": {"point_value_rub": 1.0},
        "series": [
            {"date": "2023-01-10", "spread": 100.0, "far": 31000.0},
            {"date": "2023-01-11", "spread": 104.0, "far": 31000.0, "signal_z": 1.8},
            {"date": "2023-01-12", "spread": 101.0, "far": 31000.0, "funding_rub": 2.5},
        ],
        "trades": [
            {
                "number": 1,
                "side": "long_spread",
                "entry": {
                    "date": "2023-01-10",
                    "spread_fill": 100.0,
                    "costs": {"exchange_clearing": 1.25, "broker": 0.0, "slippage": 2.0},
                },
                "exit": {
                    "date": "2023-01-11",
                    "spread_fill": 104.5,
                    "costs": {"exchange_clearing": 1.25, "broker": 0.0, "slippage": 2.0},
                },
                "net_rub": 12.0,
                "reason": "z_reverted",
            },
            {
                "number": 2,
                "side": "short_spread",
                "entry": {
                    "date": "2023-01-11",
                    "spread_fill": 105.0,
                    "costs": {"exchange_clearing": 1.0, "broker": 0.0, "slippage": 1.0},
                },
                "exit": {
                    "date": "2023-01-12",
                    "spread_fill": 105.0,
                    "costs": {"exchange_clearing": 0.0, "broker": 0.0, "slippage": 0.0},
                },
                "net_rub": 0.0,
                "reason": "end_of_window",
            },
        ],
        "ledger": [
            {"date": "2023-01-10"},
            {"date": "2023-01-11"},
            {"date": "2023-01-12"},
        ],
        "summary": {"trades": 2},
        "notes": ["base note"],
    }


class CalendarAdapterRegressionTests(unittest.TestCase):
    def test_compatibility_aliases_keep_exact_identity(self) -> None:
        self.assertIs(calendar_adapter._cost_totals, cost_totals)
        self.assertIs(calendar_adapter._run_id, run_id)

    def test_calendar_proof_enriches_payload_without_mutating_caller_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_intake(root)
            raw = _sample_payload(fill="next_open", timeframe="1d")
            raw["provenance"] = {"contracts_sha256": "override-digest", "custom": "kept"}
            snapshot = json.loads(json.dumps(raw))

            result = calendar_proof(root, {"pair": "SRH3/SRM3", "fill": "next_open"}, raw)

            self.assertEqual(set(raw), set(snapshot))
            self.assertNotIn("run_id", raw)
            self.assertEqual(result["universe"], CALENDAR_UNIVERSE)
            self.assertEqual(result["schema_version"], PROOF_SCHEMA)
            self.assertEqual(result["selection"]["universe"], CALENDAR_UNIVERSE)
            self.assertEqual(result["selection"]["timeframe"], "1d")
            self.assertFalse(result["execution_reference"])
            self.assertEqual(result["notes"], ["base note"])

            first, second = result["trades"]
            self.assertEqual(first["gross_pts"], 4.5)
            self.assertEqual(first["result"], "win")
            self.assertFalse(first["open_at_end"])
            self.assertEqual(second["gross_pts"], 0.0)
            self.assertEqual(second["result"], "flat")
            self.assertTrue(second["open_at_end"])

            self.assertEqual(
                [row["event"] for row in result["ledger"]],
                ["ENTRY #1", "EXIT #1 + ENTRY #2", "EXIT #2"],
            )
            self.assertTrue(all(row["funding_rub"] is None for row in result["ledger"]))
            self.assertTrue(all(row["lqdt_on_margin_rub"] is None for row in result["ledger"]))
            self.assertIsNone(result["series"][0]["funding_rub"])
            self.assertIsNone(result["series"][0]["signal_z"])
            self.assertEqual(result["series"][1]["signal_z"], 1.8)
            self.assertEqual(result["series"][2]["funding_rub"], 2.5)

            self.assertEqual(
                result["summary"]["cost_totals"],
                {"exchange_clearing": 3.5, "broker": 0.0, "slippage": 5.0},
            )
            self.assertEqual(result["summary"]["closed_trades"], 1)
            self.assertEqual(result["summary"]["open_positions"], 1)
            self.assertIsNone(result["summary"]["max_excess_drawdown_rub"])
            self.assertEqual(result["provenance"]["contracts_sha256"], "override-digest")
            self.assertEqual(result["provenance"]["custom"], "kept")
            self.assertIn("официальному расчёту", result["price_role"])
            self.assertEqual(
                result["run_id"],
                run_id(
                    {
                        "selection": result["selection"],
                        "daily": result["series"],
                        "source_hashes": result["provenance"],
                    }
                ),
            )

    def test_price_role_and_reference_warning_cover_h1_proxy_and_same_bar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            h1_native = _sample_payload(fill="next_open", timeframe="1h")
            h1_result = calendar_proof(root, {"fill": "next_open"}, h1_native)
            self.assertIn("Нативные часовые свечи", h1_result["price_role"])
            self.assertIn("next open", h1_result["price_role"])

            proxy = _sample_payload(fill="reference_same_bar", timeframe="1h")
            proxy["window"]["price_source"] = "synthetic_leg_proxy"
            proxy_result = calendar_proof(root, {"fill": "reference_same_bar"}, proxy)
            self.assertTrue(proxy_result["execution_reference"])
            self.assertIn(REFERENCE_WARNING, proxy_result["notes"])
            self.assertIn("Синтетический H1 proxy", proxy_result["price_role"])
            self.assertIn("reference same bar", proxy_result["price_role"])

    def test_calendar_provenance_and_instrument_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "data/spread-history-cache"
            cache.mkdir(parents=True)
            (cache / "SRH3-a.json").write_text("old", encoding="utf-8")
            (cache / "SRH3-b.json").write_text("new", encoding="utf-8")
            (cache / "SRH3SRM3-a.json").write_text("spread", encoding="utf-8")
            for relative, text in (
                ("data/benchmark/lqdt-lineage.json", "{}"),
                ("data/benchmark-cache/CBR_RUONIA.json", "{}"),
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")

            provenance = calendar_provenance(
                root,
                {"pair": "SRH3/SRM3", "spread_instrument": "SRH3SRM3"},
            )
            self.assertIn("near_history_sha256", provenance)
            self.assertNotIn("far_history_sha256", provenance)
            self.assertIn("spread_history_sha256", provenance)
            self.assertIn("benchmark_sha256", provenance)
            self.assertIn("ruonia_sha256", provenance)
            self.assertNotIn("contracts_sha256", provenance)

            _write_intake(root)
            instrument = calendar_instrument(
                root,
                {"pair": "SRH3/SRM3", "spread_instrument": "SRH3SRM3"},
                {"window": None, "contract": {"point_value_rub": 12.5}},
            )
            self.assertEqual(instrument["near"], "SRH3")
            self.assertEqual(instrument["far"], "SRM3")
            self.assertEqual(instrument["near_expiry"], "2023-03-16")
            self.assertEqual(instrument["far_expiry"], "2023-06-15")
            self.assertEqual(instrument["near_point_value_rub"], 12.5)
            self.assertEqual(instrument["far_point_value_rub"], 12.5)

            fallback_instrument = calendar_instrument(
                root,
                {"pair": "MISSING/OTHER"},
                {"window": {"near_expiry": "2023-03-01"}, "contract": None},
            )
            self.assertEqual(fallback_instrument["near_expiry"], "2023-03-01")
            self.assertIsNone(fallback_instrument["far_expiry"])


if __name__ == "__main__":
    unittest.main()
