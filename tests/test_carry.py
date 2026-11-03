import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.carry import _transaction_cost_profile, build_carry_strategy, build_carry_study


def source(secid, rows):
    return {
        "schema_version": "iss-history.v1",
        "secid": secid,
        "rows": rows,
        "source": {"raw_response_sha256": [f"{secid}-hash"]},
    }


class CarryTests(unittest.TestCase):
    def test_current_moex_taker_tariff_is_separate_from_unverified_profiles(self):
        profile = _transaction_cost_profile(
            "moex_current_taker",
            1.0,
            near_price=12.0,
            far_price=12.2,
        )
        self.assertEqual(profile["exchange_clearing_rate"], 0.0000462)
        self.assertEqual(profile["status"], "current_official_moex_taker_rate_account_broker_unverified")
        self.assertAlmostEqual(profile["round_trip_cost"], 2.23608, places=6)

        vtb = _transaction_cost_profile(
            "vtb_online_prime_current",
            1.0,
            near_price=12.0,
            far_price=12.2,
        )
        self.assertEqual(vtb["vtb_broker_commission"], 0.0)
        self.assertEqual(vtb["vtb_tariff_effective_to"], "2026-12-31")

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

    def test_strategy_uses_one_observation_execution_delay_and_lqdt_mark(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contracts = root / "contracts.json"
            contracts.write_text(json.dumps({"instruments": [
                {"secid": "NEAR", "stable_id": "n", "board": "RFUD", "expiry": "2100-01-01", "lot_size": 1000, "tick_size": 0.001, "tick_value": 1, "initial_margin": 900, "source_vintage": "current_snapshot"},
                {"secid": "FAR", "stable_id": "f", "board": "RFUD", "expiry": "2026-12-17", "lot_size": 1000, "tick_size": 0.001, "tick_value": 1, "initial_margin": 1000, "source_vintage": "current_snapshot"},
            ]}), encoding="utf-8")
            dates = [f"2026-01-{day:02d}" for day in range(1, 7)]
            carry = {
                "schema_version": "carry-study.v1",
                "near": "NEAR",
                "far": "FAR",
                "rows": [
                    {"date": dates[0], "basis_far_minus_near": 1.0, "daily_funding_received_short_near": 1.0, "daily_gross_carry_pnl": 0.0},
                    {"date": dates[1], "basis_far_minus_near": 1.0, "daily_funding_received_short_near": 1.0, "daily_gross_carry_pnl": 0.0},
                    {"date": dates[2], "basis_far_minus_near": 0.5, "daily_funding_received_short_near": 1.0, "daily_gross_carry_pnl": 0.0},
                    {"date": dates[3], "basis_far_minus_near": 0.4, "daily_funding_received_short_near": 1.0, "daily_gross_carry_pnl": 7.0},
                    {"date": dates[4], "basis_far_minus_near": 1.2, "daily_funding_received_short_near": 1.0, "daily_gross_carry_pnl": 8.0},
                    {"date": dates[5], "basis_far_minus_near": 1.2, "daily_funding_received_short_near": 1.0, "daily_gross_carry_pnl": 9.0},
                ],
            }
            carry_path = root / "carry.json"
            carry_path.write_text(json.dumps(carry), encoding="utf-8")
            benchmark = root / "benchmark.json"
            benchmark.write_text(json.dumps({
                "schema_version": "lqdt-lineage.v1",
                "rows": [{"date": date, "price": 1.0, "source_kind": "observed", "source_ticker": "LQDT"} for date in dates],
            }), encoding="utf-8")
            result = build_carry_strategy(
                carry,
                benchmark,
                contracts,
                transaction_cost_scenario="low",
                initial_capital=30000,
                warmup_observations=2,
            )

        self.assertEqual(result["summary"]["closed_trade_count"], 1)
        self.assertEqual(result["summary"]["open_trade_count"], 0)
        trade = result["trades"][0]
        self.assertEqual(trade["entry_decision_date"], dates[2])
        self.assertEqual(trade["entry_execution_date"], dates[3])
        self.assertEqual(trade["exit_decision_date"], dates[4])
        self.assertEqual(trade["exit_execution_date"], dates[5])
        self.assertEqual(trade["gross_pnl"], 17.0)
        self.assertEqual(result["daily"][2]["position"], 0)
        self.assertEqual(result["daily"][3]["position"], 1)
        self.assertEqual(result["daily"][5]["position"], 0)
        self.assertEqual(result["parameters"]["assignment_policy"], "exit_before_perpetual_execution_window")
        self.assertEqual(result["parameters"]["assignment_window_status"], "not_observable_in_input_window")

    def test_strategy_exits_when_assignment_window_is_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contracts = root / "contracts.json"
            contracts.write_text(json.dumps({"instruments": [
                {"secid": "NEAR", "stable_id": "n", "board": "RFUD", "expiry": "2100-01-01", "lot_size": 1000, "tick_size": 0.001, "tick_value": 1, "initial_margin": 900, "source_vintage": "current_snapshot"},
                {"secid": "FAR", "stable_id": "f", "board": "RFUD", "expiry": "2026-12-17", "lot_size": 1000, "tick_size": 0.001, "tick_value": 1, "initial_margin": 1000, "source_vintage": "current_snapshot"},
            ]}), encoding="utf-8")
            dates = [f"2026-12-{day:02d}" for day in range(10, 17)]
            carry = {
                "schema_version": "carry-study.v1",
                "near": "NEAR",
                "far": "FAR",
                "rows": [{
                    "date": date,
                    "basis_far_minus_near": basis,
                    "daily_funding_received_short_near": 1.0,
                    "daily_gross_carry_pnl": 1.0,
                } for date, basis in zip(dates, (1.0, 1.0, 0.5, 0.4, 0.4, 0.4, 0.4))],
            }
            benchmark = root / "benchmark.json"
            benchmark.write_text(json.dumps({
                "schema_version": "lqdt-lineage.v1",
                "rows": [{"date": date, "price": 1.0, "source_kind": "observed", "source_ticker": "LQDT"} for date in dates],
            }), encoding="utf-8")
            result = build_carry_strategy(
                carry,
                benchmark,
                contracts,
                transaction_cost_scenario="low",
                initial_capital=30000,
                warmup_observations=2,
                expiry_buffer_days=0,
                assignment_window_trading_days=2,
            )

        self.assertTrue(result["summary"]["perpetual_assignment_event_observed"])
        self.assertEqual(result["parameters"]["assignment_window_start_date"], dates[5])
        self.assertEqual(result["trades"][0]["exit_decision_date"], dates[5])
        self.assertTrue(any(signal["reason"] == "perpetual_execution_window" for signal in result["signals"]))


if __name__ == "__main__":
    unittest.main()
