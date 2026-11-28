import json
import math
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from moex_spread_lab.data import HISTORY_FIELDS, history_url
from moex_spread_lab.proof.service import ProofService
from perf_equivalence_fixtures import temporary_workspace
from synthetic_history import weekdays

MEAN_REVERSION_REQUEST = {
    "universe": "pair_screen",
    "pair": "CNYRUBF/CRZ6",
    "strategy": "mean_reversion",
    "quantity": 2,
    "slippage_ticks": 1.0,
    "parameters": {"entry_z": 1.5, "exit_z": 0.3, "lookback": 20},
}

START, END = "2025-06-17", "2026-10-27"


def _near_close(i: int) -> float:
    return 12 + 0.9 * math.sin(i * 0.55) + i * 0.012


def _far_close(i: int) -> float:
    return 13.1 + 0.9 * math.sin(i * 0.55 + 0.4) + i * 0.012


def _write_carry_history(root: Path, secid: str, days: list[date], close) -> None:
    data = []
    for i, d in enumerate(days):
        settle = close(i)
        data.append(
            [
                "RFUD",
                d.isoformat(),
                secid,
                round(settle, 4),
                round(settle, 4),
                0.015,
                100000,
                500000.0,
                5000,
                round(settle, 4),
                250,
                "CNY",
            ]
        )
    path = root / "data" / "carry-history-cache" / f"{secid}-{START}-{END}-0.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    url = history_url(secid, START, END, 0, HISTORY_FIELDS)
    payload = {
        "history": {"columns": list(HISTORY_FIELDS), "data": data},
        "history.cursor": {
            "columns": ["INDEX", "TOTAL", "PAGESIZE"],
            "data": [[0, len(data), 100]],
        },
    }
    path.write_text(
        json.dumps(
            {
                "url": url,
                "payload": payload,
                "raw_response_sha256": "ab" * 32,
                "cached_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )


def _build_root(root: Path) -> list[date]:
    days = weekdays(date(2025, 6, 17), count=55)
    (root / "data/w1/candles").mkdir(parents=True)
    (root / "data/benchmark").mkdir(parents=True)
    (root / "data/w1").mkdir(parents=True, exist_ok=True)
    (root / "data/w1/current-intake.json").write_text(
        json.dumps(
            {
                "schema_version": "iss-intake.v1",
                "instruments": [
                    {
                        "secid": secid,
                        "stable_id": secid,
                        "board": "RFUD",
                        "expiry": "2026-12-17",
                        "lot_size": 1000,
                        "tick_size": 0.001,
                        "tick_value": 1,
                        "initial_margin": 1000,
                        "source_vintage": "current_snapshot",
                    }
                    for secid in ("CNYRUBF", "CRZ6")
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "data/benchmark/lqdt-lineage.json").write_text(
        json.dumps(
            {
                "schema_version": "lqdt-lineage.v1",
                "rows": [
                    {
                        "date": day.isoformat(),
                        "price": 1 + i * 0.0004,
                        "source_kind": "observed",
                        "source_ticker": "LQDT",
                    }
                    for i, day in enumerate(days)
                ],
            }
        ),
        encoding="utf-8",
    )
    for secid, close in (("CNYRUBF", _near_close), ("CRZ6", _far_close)):
        rows = []
        for i, day in enumerate(days):
            value = close(i)
            rows.append(
                {
                    "begin": f"{day.isoformat()} 00:00:00",
                    "open": round(value - 0.06, 4),
                    "close": round(value, 4),
                    "high": round(value + 0.1, 4),
                    "low": round(value - 0.1, 4),
                    "volume": 5000,
                    "price_proxy_kind": "candle_close",
                }
            )
        (root / f"data/w1/candles/{secid}_1d.json").write_text(
            json.dumps(
                {"schema_version": "iss-candles.v1", "secid": secid, "interval": 24, "rows": rows}
            ),
            encoding="utf-8",
        )
    _write_carry_history(root, "CNYRUBF", days, _near_close)
    _write_carry_history(root, "CRZ6", days, _far_close)
    return days


class ExecutionProofTests(unittest.TestCase):
    def setUp(self):
        self.root = temporary_workspace(self, _build_root)
        self.proof = ProofService(self.root)

    def test_catalog_lists_both_universes_and_pair_availability(self):
        catalog = self.proof.catalog()
        self.assertIn("pair_screen", catalog)
        self.assertIn("universes", catalog)
        pair = next(
            item for item in catalog["pair_screen"]["pairs"] if item["id"] == "CNYRUBF/CRZ6"
        )
        self.assertEqual(pair["strategies"], {"mean_reversion": True, "funding_only": True})
        # calendar block degrades gracefully when the calendar cache is absent
        self.assertIn("assets", catalog)

    def test_mean_reversion_proof_reconciles(self):
        result = self.proof.proof(MEAN_REVERSION_REQUEST)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result["schema_version"], "execution-proof.v1")
        self.assertEqual(result["universe"], "pair_screen")
        self.assertGreaterEqual(len(result["trades"]), 1)
        summary = result["summary"]
        self.assertEqual(summary["ledger_reconciliation_rub"], 0.0)
        self.assertEqual(summary["fx_point_value_drift_rub"], None)
        self.assertEqual(summary["cost_totals"]["broker"], 0.0)
        ledger_costs = round(sum(row["costs_rub"] for row in result["ledger"]), 2)
        self.assertAlmostEqual(ledger_costs, round(summary["costs_rub"], 2), delta=0.02)
        self.assertAlmostEqual(
            result["ledger"][-1]["cumulative_net_rub"], summary["net_rub"], delta=0.02
        )
        self.assertEqual(summary["closed_trades"] + summary["open_positions"], summary["trades"])
        self.assert_funding_reconciles(result)
        components = sum(
            sum((trade["entry"]["costs"] or {}).values())
            + sum((trade["exit"]["costs"] or {}).values())
            for trade in result["trades"]
        )
        self.assertAlmostEqual(components, summary["costs_rub"], delta=0.02)
        first = result["trades"][0]
        self.assertEqual(first["entry"]["near_volume"], 5000)
        self.assertGreater(first["entry"]["costs"]["exchange_clearing"], 0)
        self.assertEqual(first["signal"]["kind"], "z")

    def assert_funding_reconciles(self, result):
        funding_rows = [row for row in result["series"] if row["funding_rub"] is not None]
        self.assertTrue(funding_rows)
        self.assertTrue(any(row["funding_rub"] != 0 for row in funding_rows))
        self.assertTrue(
            all(row["funding_swaprate_rub_per_unit"] is not None for row in funding_rows)
        )
        self.assertTrue(
            any(
                trade["funding_rub"] is not None and trade["funding_rub"] != 0
                for trade in result["trades"]
            )
        )
        for trade in result["trades"]:
            if trade["funding_rub"]:
                expected_sign = -1 if trade["direction_detail"] == "long_near_short_far" else 1
                self.assertEqual(1 if trade["funding_rub"] > 0 else -1, expected_sign)
        self.assertAlmostEqual(
            sum(row["funding_rub"] or 0 for row in result["ledger"]),
            sum(trade["funding_rub"] or 0 for trade in result["trades"]),
            delta=0.02,
        )

    def test_zero_volume_leg_is_journal_only_and_cannot_be_an_execution_date(self):
        far_path = self.root / "data/w1/candles/CRZ6_1d.json"
        payload = json.loads(far_path.read_text())
        zero_day = payload["rows"][30]["begin"][:10]
        payload["rows"][30]["volume"] = 0
        far_path.write_text(json.dumps(payload))
        result = self.proof.proof({**MEAN_REVERSION_REQUEST, "quantity": 1})
        journal_row = next(row for row in result["series"] if row["date"] == zero_day)
        journal_ledger = next(row for row in result["ledger"] if row["date"] == zero_day)
        self.assertEqual(journal_row["far_volume"], 0)
        self.assertFalse(journal_row["volume_eligible"])
        self.assertIsNone(journal_row["signal_z"])
        self.assertEqual(journal_ledger["mark_pnl_rub"], 0)
        self.assertTrue(
            all(
                trade[side]["date"] != zero_day
                for trade in result["trades"]
                for side in ("entry", "exit")
            )
        )

    def test_series_and_fills_spread_is_relative_to_far_contract_value(self):
        result = self.proof.proof(MEAN_REVERSION_REQUEST)
        far_mult = 1 / 0.001  # tick_value / tick_size from the fixture's current-intake.json
        for row in result["series"]:
            self.assertAlmostEqual(
                row["spread_rel"], row["spread"] / (row["far"] * far_mult) * 100, delta=1e-4
            )
            self.assertAlmostEqual(
                row["spread_open_rel"],
                (row["near_open"] * far_mult - row["far_open"] * far_mult)
                / (row["far_open"] * far_mult)
                * 100,
                delta=1e-4,
            )
        for trade in result["trades"]:
            for side in ("entry", "exit"):
                block = trade[side]
                self.assertAlmostEqual(
                    block["spread_fill_rel"],
                    block["spread_fill"] / (block["far_price"] * far_mult) * 100,
                    delta=1e-4,
                )

    def test_deterministic_run_id_and_parameter_sensitivity(self):
        base = {**MEAN_REVERSION_REQUEST, "quantity": 1}
        first = self.proof.proof(base)
        self.assertEqual(first["run_id"], self.proof.proof(base)["run_id"])
        other = self.proof.proof({**base, "slippage_ticks": 2.0})
        self.assertNotEqual(first["run_id"], other["run_id"])


if __name__ == "__main__":
    unittest.main()
