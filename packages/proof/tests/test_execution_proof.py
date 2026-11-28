import json
import unittest

from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.proof.service import ProofService
from perf_equivalence_fixtures import temporary_workspace
from workspace_fixtures import END, START, build_pair_root

MEAN_REVERSION_REQUEST = {
    "universe": "pair_screen",
    "pair": "CNYRUBF/CRZ6",
    "strategy": "mean_reversion",
    "quantity": 2,
    "slippage_ticks": 1.0,
    "parameters": {"entry_z": 1.5, "exit_z": 0.3, "lookback": 20},
}


class ExecutionProofTests(unittest.TestCase):
    def setUp(self):
        self.root = temporary_workspace(self, build_pair_root)
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

    def test_carry_auto_sizing_preserves_sub_kopeck_unit_funding(self):
        cache_path = self.root / "data/carry-history-cache" / f"CNYRUBF-{START}-{END}-0.json"
        doc = json.loads(cache_path.read_text())
        sr_idx = doc["payload"]["history"]["columns"].index("SWAPRATE")
        for row in doc["payload"]["history"]["data"]:
            row[sr_idx] = 0.000004
        cache_path.write_text(json.dumps(doc))
        settings = ResearchSettings(initial_capital_rub=100_000.0)
        auto_req = {
            "universe": "pair_screen",
            "pair": "CNYRUBF/CRZ6",
            "strategy": "funding_only",
            "sizing": "max_affordable",
            "initial_capital_rub": 100_000.0,
            "slippage_ticks": 1.0,
            "parameters": {"lookback": 10},
        }
        prep_auto = self.proof.prepare(auto_req, settings=settings)
        auto_res = prep_auto(auto_req, metrics_only=False)
        fast_res = prep_auto(auto_req, metrics_only=True)
        qty = auto_res["trades"][0]["quantity"]
        fixed_req = {**auto_req, "quantity": qty}
        fixed_req.pop("sizing")
        fixed_res = self.proof.prepare(fixed_req, settings=settings)(fixed_req)
        auto_fund = round(sum(r["funding_rub"] or 0 for r in auto_res["ledger"]), 2)
        fixed_fund = round(sum(r["funding_rub"] or 0 for r in fixed_res["ledger"]), 2)
        self.assertGreater(auto_fund, 0.0)
        self.assertEqual(auto_fund, fixed_fund)
        self.assertEqual(
            [r["funding_rub"] for r in auto_res["series"]],
            [r["funding_rub"] for r in fixed_res["series"]],
        )
        self.assertEqual(
            auto_res["ledger"][-1]["cumulative_net_rub"],
            fast_res["ledger"][-1]["cumulative_net_rub"],
        )


if __name__ == "__main__":
    unittest.main()
