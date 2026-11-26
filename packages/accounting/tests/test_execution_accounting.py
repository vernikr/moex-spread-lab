import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.accounting.account_risk import (
    AccountRiskError,
    assess_vtb_pair,
    ebs_undercovered_margin_charge,
)
from moex_spread_lab.accounting.costs import cost_totals, side_cost, tariff_profile
from moex_spread_lab.accounting.ledger import build_ledger
from moex_spread_lab.accounting.portfolio import lqdt_overlay_values
from moex_spread_lab.core.w0 import W0Error

CONTRACT = {
    "secid": "SBERF",
    "lot_size": 100,
    "tick_value": 1.0,
    "tick_size": 0.01,
    "initial_margin": 1000.0,
}


class CostTests(unittest.TestCase):
    def test_tariff_uses_group_rate_on_contract_value_and_slippage_ticks(self):
        cost = side_cost(tariff_profile(2.0), [(CONTRACT, 300.0)], 3)
        self.assertAlmostEqual(cost["exchange_clearing"], 300 * 100 * 0.0003 * 3)
        self.assertEqual(cost["slippage"], 6.0)
        self.assertEqual(cost["broker"], 0.0)
        totals = cost_totals([{"entry": {"costs": cost}, "exit": {"costs": cost}}])
        self.assertEqual(totals["slippage"], 12.0)
        self.assertAlmostEqual(totals["exchange_clearing"], 300 * 100 * 0.0003 * 6)

    def test_unknown_contract_and_bad_slippage_are_rejected(self):
        with self.assertRaises(W0Error):
            side_cost(tariff_profile(), [({**CONTRACT, "secid": "XX"}, 1.0)], 1)
        with self.assertRaises(W0Error):
            tariff_profile(-1)


class OverlayTests(unittest.TestCase):
    def test_free_cash_compounds_and_margin_idles(self):
        flat, _ = lqdt_overlay_values(1000.0, [0, 0, 0], [0, 0, 0], [1.0, 1.1, 1.21])
        self.assertAlmostEqual(flat[-1], 1210.0)
        held, shortfalls = lqdt_overlay_values(1000.0, [0, 0, 0], [0, 500, 500], [1.0, 1.1, 1.21])
        self.assertAlmostEqual(held[-1], 660.0 + 500.0)
        self.assertLess(held[-1], flat[-1])
        self.assertEqual(shortfalls, 0)

    def test_loss_beyond_free_cash_is_flagged(self):
        _, shortfalls = lqdt_overlay_values(100.0, [0, -150], [0, 80], [1.0, 1.0])
        self.assertEqual(shortfalls, 1)


class AccountRiskTests(unittest.TestCase):
    def test_ebs_undercovered_charge_and_pair_stress_screen(self):
        charge = ebs_undercovered_margin_charge(12_000.0, 10_000.0, key_rate=0.14, days=5)
        self.assertAlmostEqual(charge, 2_000.0 * 0.16 * 5 / 365.0)
        self.assertEqual(ebs_undercovered_margin_charge(5_000.0, 10_000.0), 0.0)
        with self.assertRaises(AccountRiskError):
            ebs_undercovered_margin_charge(-1.0, 0.0)

        with tempfile.TemporaryDirectory() as tmp:
            contracts_path = Path(tmp) / "current-intake.json"
            contracts_path.write_text(
                json.dumps(
                    {
                        "schema_version": "iss-intake.v1",
                        "instruments": [
                            {
                                "secid": "SBERF",
                                "stable_id": "SBERF",
                                "board": "RFUD",
                                "expiry": "2026-12-17",
                                "lot_size": 100,
                                "tick_size": 0.01,
                                "tick_value": 1.0,
                                "initial_margin": 1000.0,
                                "source_vintage": "current_snapshot",
                            },
                            {
                                "secid": "SRZ6",
                                "stable_id": "SRZ6",
                                "board": "RFUD",
                                "expiry": "2026-12-17",
                                "lot_size": 100,
                                "tick_size": 1.0,
                                "tick_value": 1.0,
                                "initial_margin": 1500.0,
                                "source_vintage": "current_snapshot",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            passed = assess_vtb_pair(contracts_path, "SBERF", "SRZ6", initial_capital=30_000.0)
            self.assertTrue(passed["summary"]["all_scenarios_passed"])
            failed = assess_vtb_pair(contracts_path, "SBERF", "SRZ6", initial_capital=12_000.0)
            self.assertFalse(failed["summary"]["all_scenarios_passed"])


class OpenFillTests(unittest.TestCase):
    def test_delayed_entry_and_exit_attempts_project_costs_and_lqdt_overlay(self):
        profile = tariff_profile(1.0)
        near_contract = {**CONTRACT, "tick_size": 1.0}
        far_contract = {**CONTRACT, "secid": "SRZ6", "tick_size": 1.0}
        entry_costs = side_cost(profile, [(near_contract, 10.0), (far_contract, 14.0)], 1)
        exit_costs = side_cost(profile, [(near_contract, 10.0), (far_contract, 10.2)], 1)
        total_costs = round(sum(entry_costs.values()) + sum(exit_costs.values()), 8)
        overlay, shortfalls = lqdt_overlay_values(
            30_000.0,
            [0.0, -sum(entry_costs.values()), 3.8 - total_costs],
            [0.0, 2_000.0, 0.0],
            [1.0, 1.001, 1.002],
        )
        self.assertEqual(shortfalls, 0)
        trade_cost_totals = cost_totals(
            [{"entry": {"costs": entry_costs}, "exit": {"costs": exit_costs}}]
        )
        baseline = {
            "schema_version": "analytical-baseline.v1",
            "analysis_role": "diagnostic_only",
            "near": "SBERF",
            "far": "SRZ6",
            "timeframe": "1d",
            "execution_evidence_status": "proxy_only",
            "parameters": {
                "decision_delay_bars": 1,
                "quantity": 1,
                "initial_capital": 30_000.0,
                "accounting": "free_cash_in_lqdt_margin_cash_idle",
            },
            "economics": {"pair_initial_margin_rub": 2_000.0},
            "costs": {"total_by_component": trade_cost_totals},
            "daily": [
                {
                    "date": "2026-03-23",
                    "timestamp": "2026-03-23 00:00:00",
                    "position": 0,
                    "strategy_value": overlay[0],
                    "benchmark_value": 30_000.0,
                    "X": overlay[0] - 30_000.0,
                    "H": 0.0,
                    "D": 0.0,
                    "modeled_costs": 0.0,
                    "gross_realized_pnl": 0.0,
                },
                {
                    "date": "2026-03-24",
                    "timestamp": "2026-03-24 00:00:00",
                    "position": 1,
                    "strategy_value": overlay[1],
                    "benchmark_value": 30_030.0,
                    "X": overlay[1] - 30_030.0,
                    "H": 0.0,
                    "D": max(0.0, 30_030.0 - overlay[1]),
                    "modeled_costs": round(sum(entry_costs.values()), 8),
                    "gross_realized_pnl": 0.0,
                },
                {
                    "date": "2026-03-25",
                    "timestamp": "2026-03-25 00:00:00",
                    "position": 0,
                    "strategy_value": overlay[2],
                    "benchmark_value": 30_060.0,
                    "X": overlay[2] - 30_060.0,
                    "H": max(0.0, overlay[2] - 30_060.0),
                    "D": max(0.0, 30_060.0 - overlay[2]),
                    "modeled_costs": total_costs,
                    "gross_realized_pnl": 3.8,
                },
            ],
            "trades": [
                {
                    "entry_date": "2026-03-24 00:00:00",
                    "exit_date": "2026-03-25 00:00:00",
                    "quantity": 1,
                    "direction": "long_spread",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            baseline_path = Path(tmp) / "baseline.json"
            baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
            ledger = build_ledger(baseline_path)

        self.assertEqual(ledger["summary"]["execution_attempt_count"], 2)
        self.assertEqual(ledger["summary"]["portfolio_mark_count"], 3)
        attempts = [e["payload"] for e in ledger["events"] if e["kind"] == "execution_attempt"]
        self.assertEqual(attempts[0]["signal_at"], "2026-03-23 00:00:00")
        self.assertEqual(attempts[0]["decision_at"], "2026-03-24 00:00:00")
        self.assertEqual(ledger["projection"][1]["reserved_margin_proxy"], 2_000.0)
        self.assertGreater(ledger["portfolio"]["costs"]["total_by_component"]["slippage"], 0)


if __name__ == "__main__":
    unittest.main()
