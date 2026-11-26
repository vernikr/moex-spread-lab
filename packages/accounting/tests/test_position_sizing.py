"""Automatic proof sizing: finite cash, causality, costs and settings validation."""

import math
import tempfile
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.position_sizing import maximum_quantity, size_proof
from moex_spread_lab.core.settings import (
    ResearchSettings,
    read_settings,
    save_settings,
    settings_path,
)
from moex_spread_lab.core.w0 import W0Error


def unit_proof(near=(50000, 49000, 49000, 49200), entries=((0, 1), (2, 3)), *, margin=5000):
    days = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(len(near))]
    rows = [
        {
            "date": d,
            "near": n,
            "far": 50000,
            "spread": n - 50000,
            "near_open": n,
            "far_open": 50000,
            "volume_eligible": True,
            "funding_rub": None,
            "funding_contracts": 0,
        }
        for d, n in zip(days, near)
    ]
    trades = []
    for number, (a, b) in enumerate(entries, 1):

        def fill(i):
            return {
                "date": days[i],
                "near_price": near[i],
                "far_price": 50000,
                "spread_fill": near[i] - 50000,
                "costs": {},
            }

        trades.append(
            {
                "number": number,
                "quantity": 1,
                "side": "long_spread",
                "entry": fill(a),
                "exit": fill(b),
                "margin_rub": margin,
                "open_at_end": False,
                "signal": {"kind": "synthetic", "date": days[a]},
            }
        )
    proof = {
        "run_id": "unit-intents",
        "universe": "pair_screen",
        "selection": {
            "universe": "pair_screen",
            "strategy": "mean_reversion",
            "parameters": {},
            "fill": "next_open",
        },
        "instrument": {"near": "SRZ6", "far": "GZZ6"},
        "contract": {},
        "window": {"passes_capital_screen": True},
        "series": rows,
        "trades": trades,
        "summary": {},
        "notes": [],
    }
    contracts = {
        s: {
            "secid": s,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "initial_margin": margin / 2,
            "tariff_group": "stock",
        }
        for s in ("SRZ6", "GZZ6")
    }
    return proof, dict.fromkeys(days, 1.0), contracts


def native_proof(*, entries=((0, 1),)):
    proof, marks, _ = unit_proof(near=(50000, 50000, 50000), entries=entries)
    proof["universe"] = proof["selection"]["universe"] = "calendar_native"
    proof["selection"]["strategy"] = "zscore_mean_reversion"
    proof["contract"] = {
        "margin_rate": 0.005,
        "point_value_rub": 3.0,
        "tick_size": 2.0,
        "tariff_group": "stock",
    }
    for row in proof["series"]:
        row["point_value_rub"] = 3.0
    return proof, marks


class MaximumQuantityTests(unittest.TestCase):
    def test_exact_capital_boundary_fees_stress_and_no_form_ceiling(self):
        self.assertEqual(maximum_quantity(30000, 5000, {"fee": 0}, stress=False), 6)
        self.assertEqual(maximum_quantity(30000, 5000, {"fee": 0.01}, stress=False), 5)
        self.assertEqual(maximum_quantity(30000, 5000, {"fee": 10}, stress=True), 1)
        self.assertEqual(maximum_quantity(30000, 31000, {"fee": 0}, stress=False), 0)
        self.assertEqual(maximum_quantity(0, 5000, {"fee": 0}, stress=False), 0)
        self.assertEqual(maximum_quantity(30000, 100, {"fee": 0}, stress=False), 300)
        bad_margins: tuple[Any, ...] = (0, None, -1, math.inf, math.nan, True)
        for margin in bad_margins:
            with self.subTest(margin=margin), self.assertRaises(W0Error):
                maximum_quantity(30000, margin, {"fee": 0}, stress=False)

    def test_stress_preserves_the_two_engines_uncertainty_buffer_conventions(self):
        # Pair account: 3*M*q + 5k*q + 5k. Native: (3*M + 5k + 5k)*q.
        self.assertEqual(maximum_quantity(30000, 100, {}, stress=True), 4)
        self.assertEqual(maximum_quantity(30000, 100, {}, stress=True, native_spread=True), 2)

    def test_every_returned_integer_is_maximal_after_component_rounding(self):
        for budget in (1, 100, 5000, 29999.99, 30000):
            for margin in (0.1, 15.03, 5000):
                for fee in (0, 0.001, 0.0049, 0.015, 32.79):
                    costs = {"exchange_clearing": fee, "slippage": 0.5}
                    q = maximum_quantity(budget, margin, costs, stress=False)
                    required = lambda n, margin=margin, costs=costs: (
                        n * margin + 2 * sum(round(v * n, 2) for v in costs.values())
                    )
                    self.assertLessEqual(required(q), budget + 1e-9)
                    self.assertGreater(required(q + 1), budget + 1e-9)


class FundedSizingTests(unittest.TestCase):
    def replay(self, proof, marks, contracts=None, settings=None):
        return size_proof(proof, marks, settings or ResearchSettings(), contracts or {})

    def assert_reconciles(self, proof):
        self.assertEqual(proof["summary"]["ledger_reconciliation_rub"], 0)
        self.assertAlmostEqual(
            sum(t["gross_rub"] for t in proof["trades"]),
            sum(r["mark_pnl_rub"] for r in proof["ledger"]),
            places=6,
        )
        self.assertAlmostEqual(
            sum(t["costs_rub"] for t in proof["trades"]),
            sum(r["costs_rub"] for r in proof["ledger"]),
            places=6,
        )
        self.assertEqual(proof["summary"]["net_rub"], proof["ledger"][-1]["cumulative_net_rub"])

    def test_losses_resize_the_next_entry_but_never_an_open_position(self):
        unit, marks, contracts = unit_proof()
        proof = self.replay(unit, marks, contracts)
        self.assertEqual([t["quantity"] for t in proof["trades"]], [5, 4])
        self.assertEqual([r["quantity"] for r in proof["ledger"]], [5, 0, 4, 0])
        self.assertLess(proof["trades"][1]["sizing"]["available_capital_rub"], 25000)
        self.assertEqual(proof["sizing"]["additional_funding_rub"], 0)
        self.assertEqual(unit["trades"][0]["quantity"], 1)  # immutable intents
        self.assert_reconciles(proof)

    def test_profits_do_not_expand_the_original_entry_envelope(self):
        unit, marks, contracts = unit_proof(near=(50000, 51000, 51000, 51000))
        proof = self.replay(unit, marks, contracts)
        self.assertGreater(proof["ledger"][1]["account_value_rub"], 30000)
        self.assertEqual([t["quantity"] for t in proof["trades"]], [5, 5])
        self.assertEqual(proof["trades"][1]["sizing"]["available_capital_rub"], 30000)
        self.assert_reconciles(proof)

    def test_unaffordable_intents_are_zero_exposure_not_fictional_trades(self):
        unit, marks, contracts = unit_proof(margin=31000)
        proof = self.replay(unit, marks, contracts)
        self.assertFalse(proof["trades"])
        self.assertEqual(len(proof["skipped_entries"]), 2)
        self.assertTrue(all(t["quantity"] == 0 and t["reason"] for t in proof["skipped_entries"]))
        self.assertTrue(
            all(r["quantity"] == r["costs_rub"] == r["mark_pnl_rub"] == 0 for r in proof["ledger"])
        )
        self.assertTrue(all(r["account_value_rub"] == 30000 for r in proof["ledger"]))
        self.assert_reconciles(proof)

    def test_benchmark_relative_drawdown_no_longer_stops_later_entries(self):
        # The 10k excess-drawdown trigger is gone: a book that falls behind LQDT
        # keeps trading. Only a cash shortfall can stop new entries.
        unit, marks, contracts = unit_proof(near=(50000, 47800, 47800, 48000))
        proof = self.replay(unit, marks, contracts)
        self.assertEqual(len(proof["trades"]), 2)
        self.assertEqual(proof["skipped_entries"], [])
        self.assertGreaterEqual(proof["summary"]["max_excess_drawdown_rub"], 10000)
        self.assert_reconciles(proof)

    def test_stress_preference_changes_size_without_changing_raw_window_verdict(self):
        unit, marks, contracts = unit_proof()
        proof = self.replay(unit, marks, contracts, ResearchSettings(allow_failed_capital=False))
        self.assertTrue(proof["window"]["passes_capital_screen"])
        self.assertEqual([t["quantity"] for t in proof["trades"]], [1, 1])
        self.assert_reconciles(proof)

    def test_native_slippage_uses_actual_tick_value_once_per_side(self):
        unit, marks = native_proof()
        proof = self.replay(unit, marks, settings=ResearchSettings(slippage_ticks=2.5))
        t = proof["trades"][0]
        # 3 RUB/point * 2 points/tick; one native spread book, not two legs.
        self.assertEqual(t["entry"]["costs"]["slippage"], 2.5 * 6 * t["quantity"])
        self.assertEqual(proof["summary"]["cost_totals"]["slippage"], 2 * 2.5 * 6 * t["quantity"])
        self.assert_reconciles(proof)

    def test_entry_size_does_not_use_its_close_future_exit_fx_or_benchmark_return(self):
        unit, marks = native_proof(entries=((1, 2),))
        base = self.replay(unit, marks)
        changed = deepcopy(unit)
        changed["series"][1]["point_value_rub"] = 30.0  # same-day FX close, after entry
        changed["series"][1]["spread"] = 1000000
        changed["trades"][0]["exit"]["far_price"] = 10000000
        changed_marks = dict(marks)
        changed_marks[changed["series"][1]["date"]] = 100.0
        altered = self.replay(changed, changed_marks)
        self.assertEqual(base["trades"][0]["quantity"], altered["trades"][0]["quantity"])
        self.assertEqual(
            base["trades"][0]["entry"]["costs"], altered["trades"][0]["entry"]["costs"]
        )

    def test_funding_uses_held_quantity_and_is_zero_on_an_exit_before_clearing(self):
        unit, marks, contracts = unit_proof(near=(50000, 50000, 50000, 50000))
        for i, row in enumerate(unit["series"]):
            row["funding_rub"] = 2.0 if i % 2 == 0 else 0.0
            row["funding_contracts"] = 1 if i % 2 == 0 else 0
        proof = self.replay(unit, marks, contracts)
        for t in proof["trades"]:
            self.assertEqual(t["funding_rub"], 2 * t["quantity"])
        self.assertEqual(proof["ledger"][1]["funding_rub"], 0)
        self.assert_reconciles(proof)

    def test_automatic_account_has_no_former_fifty_contract_ceiling(self):
        unit, marks, contracts = unit_proof(near=(50000, 50000), entries=((0, 1),), margin=100)
        p = self.replay(unit, marks, contracts)
        self.assertGreater(p["trades"][0]["quantity"], 50)
        self.assert_reconciles(p)

    def test_native_margin_is_recomputed_at_each_entry_not_from_the_future_maximum(
        self,
    ):
        unit, marks = native_proof(entries=((0, 1), (1, 2)))
        unit["series"][1].update(near=200000, far=200000)
        unit["trades"][0]["exit"].update(near_price=200000, far_price=200000)
        unit["trades"][1]["entry"].update(near_price=200000, far_price=200000)
        p = self.replay(unit, marks)
        self.assertEqual([t["quantity"] for t in p["trades"]], [18, 3])
        self.assertEqual([t["sizing"]["margin_per_pair_rub"] for t in p["trades"]], [1500, 6000])
        self.assert_reconciles(p)

    def test_native_next_open_exit_never_uses_that_days_later_fx_fixing(self):
        unit, marks = native_proof(entries=((0, 2),))
        unit["series"][1]["spread"] = 100
        unit["series"][1]["point_value_rub"] = 4
        unit["series"][2]["point_value_rub"] = 30
        unit["trades"][0]["exit"]["spread_fill"] = 200
        p = self.replay(unit, marks)
        t = p["trades"][0]
        self.assertEqual(p["ledger"][1]["mark_pnl_rub"], 100 * 4 * t["quantity"])
        self.assertEqual(p["ledger"][2]["mark_pnl_rub"], 100 * 4 * t["quantity"])
        self.assertEqual(t["exit"]["costs"]["slippage"], 2 * 4 * t["quantity"])
        self.assert_reconciles(p)

    def test_journal_only_rows_preserve_exposure_without_price_or_margin_revaluation(
        self,
    ):
        unit, marks = native_proof(entries=((0, 2),))
        unit["series"][1].update(volume_eligible=False, spread=1000000, near=1000000, far=1000000)
        p = self.replay(unit, marks)
        self.assertEqual(p["ledger"][0]["posted_margin_rub"], p["ledger"][1]["posted_margin_rub"])
        self.assertEqual(p["ledger"][1]["mark_pnl_rub"], 0)
        self.assertEqual(p["ledger"][0]["quantity"], p["ledger"][1]["quantity"])
        self.assertEqual(p["summary"]["cash_shortfall_rows"], 0)
        self.assert_reconciles(p)

    def test_prior_exit_changes_same_row_resources_before_resizing(self):
        unit, marks, contracts = unit_proof(entries=((0, 1), (1, 3)))
        p = self.replay(unit, marks, contracts)
        self.assertEqual([t["quantity"] for t in p["trades"]], [5, 4])
        self.assertIn("EXIT #1 + ENTRY #2", p["ledger"][1]["event"])
        self.assert_reconciles(p)
        # The same-row exit frees margin, so the next entry is sized on the new
        # resources; crossing 10k behind LQDT no longer stops it.
        unit, marks, contracts = unit_proof(
            near=(50000, 47800, 47800, 48000), entries=((0, 1), (1, 3))
        )
        p = self.replay(unit, marks, contracts)
        self.assertEqual(len(p["trades"]), 2)
        self.assertEqual(p["skipped_entries"], [])
        self.assert_reconciles(p)

    def test_rejected_funding_intents_cannot_receive_unit_portfolios_payments(self):
        unit, marks, contracts = unit_proof(margin=31000)
        for row in unit["series"]:
            row.update(funding_rub=300, funding_contracts=1)
        p = self.replay(unit, marks, contracts)
        self.assertTrue(all(r["funding_rub"] == r["funding_contracts"] == 0 for r in p["series"]))
        self.assertEqual(p["summary"]["gross_rub"], 0)
        self.assert_reconciles(p)

    def test_terminal_mark_retains_final_funding_and_the_existing_cost_convention(self):
        for funding_only in (False, True):
            with self.subTest(funding_only=funding_only):
                unit, marks, contracts = unit_proof(
                    near=(50000, 50000, 50000, 50000), entries=((0, 3),)
                )
                unit["trades"][0]["open_at_end"] = True
                unit["series"][-1].update(funding_rub=2, funding_contracts=1)
                if funding_only:
                    unit["selection"].update(strategy="funding_only", fill="same_day_settlement")
                p = self.replay(unit, marks, contracts)
                t = p["trades"][0]
                self.assertEqual(p["summary"]["open_positions"], 1)
                self.assertEqual(t["funding_rub"], 2 * t["quantity"])
                self.assertEqual(
                    t["costs_rub"],
                    sum(t["entry"]["costs"].values()) * (1 if funding_only else 2),
                )
                self.assert_reconciles(p)


class SizingSettingsTests(unittest.TestCase):
    def test_settings_migrate_persist_and_reject_invalid_slippage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = settings_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            legacy = '{"validation_percent": 12.5, "allow_failed_volume": false}\n'
            path.write_text(legacy)
            self.assertEqual(read_settings(root)["slippage_ticks"], 1.0)
            self.assertEqual(path.read_text(), legacy)
            save_settings(root, {"slippage_ticks": 2.5})
            self.assertEqual(read_settings(root)["validation_percent"], 12.5)
            self.assertFalse(read_settings(root)["allow_failed_volume"])
            for value in (True, False, None, "1", -0.1, 10.1, math.inf, math.nan):
                with self.subTest(value=value), self.assertRaises(W0Error):
                    save_settings(root, {"slippage_ticks": value})
            self.assertEqual(read_settings(root)["slippage_ticks"], 2.5)
