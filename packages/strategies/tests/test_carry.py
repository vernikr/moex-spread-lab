"""Carry-study accounting, carry-strategy timing, and retired CLI surface."""

from __future__ import annotations

import argparse
import importlib
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from moex_spread_lab.accounting.account_risk import assess_vtb_pair, ebs_undercovered_margin_charge
from moex_spread_lab.core.contracts import StrategyIntentRun, WindowSnapshot
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.cli import register as register_strategy_cli
from moex_spread_lab.strategies.funding_carry.engine import build_carry_strategy
from moex_spread_lab.strategies.funding_carry.engine import (
    transaction_cost_profile as _transaction_cost_profile,
)
from moex_spread_lab.strategies.funding_carry.intents import carry_settlement_intents
from moex_spread_lab.strategies.funding_carry.plugin import (
    PLUGIN as carry_plugin,
)
from moex_spread_lab.strategies.funding_carry.plugin import (
    FundingCarryPlugin,
)
from moex_spread_lab.strategies.funding_carry.study import (
    CarryError,
    CarryStrategyError,
    build_carry_study,
)
from strategy_fixtures import write_lqdt_lineage, write_near_far_contracts

STRATEGY_KWARGS = {
    "transaction_cost_scenario": "low",
    "initial_capital": 30000,
    "warmup_observations": 2,
}


def source(secid, rows):
    return {
        "schema_version": "iss-history.v1",
        "secid": secid,
        "rows": rows,
        "source": {"raw_response_sha256": [f"{secid}-hash"]},
    }


def _carry_study(dates, *, basis, funding, gross):
    return {
        "schema_version": "carry-study.v1",
        "near": "NEAR",
        "far": "FAR",
        "rows": [
            {
                "date": day,
                "basis_far_minus_near": basis_value,
                "daily_funding_received_short_near": funding_value,
                "daily_gross_carry_pnl": gross_value,
            }
            for day, basis_value, funding_value, gross_value in zip(
                dates, basis, funding, gross, strict=True
            )
        ],
    }


class CarryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.contracts = write_near_far_contracts(self.root / "contracts.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _strategy(self, carry, dates, **kwargs):
        benchmark = write_lqdt_lineage(self.root / "benchmark.json", dict.fromkeys(dates, 1.0))
        return build_carry_strategy(
            carry, benchmark, self.contracts, **{**STRATEGY_KWARGS, **kwargs}
        )

    def test_vtb_account_risk_screen_uses_no_offset_and_stress_buffers(self):
        result = assess_vtb_pair(
            self.contracts,
            "NEAR",
            "FAR",
            observed_daily_pnl=[-450.0, 20.0],
        )

        self.assertEqual(result["pair"]["pair_margin_no_offset_rub"], 1900.0)
        self.assertEqual(result["stress_inputs"]["effective_vm_buffer_rub"], 5000.0)
        self.assertTrue(result["summary"]["all_scenarios_passed"])
        self.assertAlmostEqual(
            ebs_undercovered_margin_charge(1900.0, 0.0, key_rate=0.14), 0.8328767, places=6
        )

    def test_current_moex_taker_tariff_is_separate_from_unverified_profiles(self):
        profile = _transaction_cost_profile(
            "moex_current_taker",
            1.0,
            near_price=12.0,
            far_price=12.2,
        )
        self.assertEqual(profile["exchange_clearing_rate"], 0.0000462)
        self.assertEqual(
            profile["status"], "current_official_moex_taker_rate_account_broker_unverified"
        )
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
        result = build_carry_study(
            source(
                "NEAR",
                [
                    {"TRADEDATE": "2026-01-01", "SETTLEPRICE": 10.0, "SWAPRATE": 0.004},
                    {"TRADEDATE": "2026-01-02", "SETTLEPRICE": 10.1, "SWAPRATE": 0.005},
                ],
            ),
            source(
                "FAR",
                [
                    {"TRADEDATE": "2026-01-01", "SETTLEPRICE": 10.2, "SWAPRATE": 0.0},
                    {"TRADEDATE": "2026-01-02", "SETTLEPRICE": 10.25, "SWAPRATE": 0.0},
                ],
            ),
            self.contracts,
            near="NEAR",
            far="FAR",
        )

        self.assertEqual(result["summary"]["total_basis_pnl"], -50.0)
        self.assertEqual(result["summary"]["total_funding_received"], 5.0)
        self.assertEqual(result["summary"]["total_gross_carry_pnl"], -45.0)
        self.assertFalse(result["summary"]["transaction_costs_included"])

    def test_carry_study_normalizes_different_leg_price_units(self):
        contracts = write_near_far_contracts(
            self.root / "scaled_contracts.json",
            near={"lot_size": 100, "tick_size": 0.5, "tick_value": 5},
            far={"lot_size": 1, "tick_size": 0.05, "tick_value": 0.5},
        )
        result = build_carry_study(
            source(
                "NEAR",
                [
                    {"TRADEDATE": "2026-01-01", "SETTLEPRICE": 10.0, "SWAPRATE": 0.1},
                    {"TRADEDATE": "2026-01-02", "SETTLEPRICE": 10.1, "SWAPRATE": 0.1},
                ],
            ),
            source(
                "FAR",
                [
                    {"TRADEDATE": "2026-01-01", "SETTLEPRICE": 20.0, "SWAPRATE": 0.0},
                    {"TRADEDATE": "2026-01-02", "SETTLEPRICE": 20.2, "SWAPRATE": 0.0},
                ],
            ),
            contracts,
            near="NEAR",
            far="FAR",
        )

        self.assertEqual(result["summary"]["total_basis_pnl"], 1.0)
        self.assertEqual(result["summary"]["total_funding_received"], 10.0)
        self.assertEqual(result["summary"]["total_gross_carry_pnl"], 11.0)
        self.assertEqual(result["rows"][1]["funding_cap_status"], "not_modelled")

    def test_strategy_uses_one_observation_execution_delay_and_lqdt_mark(self):
        dates = [f"2026-01-{day:02d}" for day in range(1, 7)]
        carry = _carry_study(
            dates,
            basis=(1.0, 1.0, 0.5, 0.4, 1.2, 1.2),
            funding=(1.0,) * 6,
            gross=(0.0, 0.0, 0.0, 7.0, 8.0, 9.0),
        )
        result = self._strategy(carry, dates)

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
        self.assertEqual(
            result["parameters"]["assignment_policy"], "exit_before_perpetual_execution_window"
        )
        self.assertEqual(
            result["parameters"]["assignment_window_status"], "not_observable_in_input_window"
        )
        self.assertFalse(result["parameters"]["assignment_event"]["payment_modelled"])
        self.assertTrue(result["summary"]["account_risk_gate_current_snapshot"])

    def test_funding_only_same_row_fill_forgoes_exit_day_funding_and_scales_quantity(self):
        dates = [f"2026-01-{day:02d}" for day in range(1, 7)]
        carry = _carry_study(
            dates,
            basis=(1.0,) * 6,
            funding=(1.0, 1.0, 1.0, 1.0, -5.0, 3.0),
            gross=(0.0, 0.0, 0.0, 7.0, 8.0, 9.0),
        )
        kwargs = {
            "strategy_variant": "funding_only",
            "execution_delay_rows": 0,
            "cash_in_lqdt": True,
        }
        one = self._strategy(carry, dates, **kwargs)
        two = self._strategy(carry, dates, quantity=2, **kwargs)
        with self.assertRaises(CarryStrategyError):
            self._strategy(carry, dates, **{**kwargs, "strategy_variant": "basis_median"})

        trade = one["trades"][0]
        self.assertEqual(
            (trade["entry_execution_date"], trade["exit_execution_date"]), (dates[2], dates[5])
        )
        self.assertEqual(trade["gross_pnl"], 7.0 + 8.0 + (9.0 - 3.0))
        self.assertEqual(two["trades"][0]["gross_pnl"], 2 * trade["gross_pnl"])
        self.assertEqual(one["summary"]["cash_shortfall_rows"], 0)

    def test_strategy_exits_when_assignment_window_is_observed(self):
        dates = [f"2026-12-{day:02d}" for day in range(10, 17)]
        carry = _carry_study(
            dates,
            basis=(1.0, 1.0, 0.5, 0.4, 0.4, 0.4, 0.4),
            funding=(1.0,) * 7,
            gross=(1.0,) * 7,
        )
        result = self._strategy(
            carry, dates, expiry_buffer_days=0, assignment_window_trading_days=2
        )

        self.assertTrue(result["summary"]["perpetual_assignment_event_observed"])
        self.assertEqual(result["parameters"]["assignment_window_start_date"], dates[5])
        self.assertEqual(result["trades"][0]["exit_decision_date"], dates[5])
        self.assertTrue(
            any(signal["reason"] == "perpetual_execution_window" for signal in result["signals"])
        )

    def test_funding_only_hypothesis_has_separate_rule_id_and_entry_condition(self):
        dates = [f"2026-01-{day:02d}" for day in range(1, 7)]
        carry = _carry_study(dates, basis=(1.0,) * 6, funding=(1.0,) * 6, gross=(1.0,) * 6)
        result = self._strategy(carry, dates, strategy_variant="funding_only")

        self.assertEqual(result["rule_id"], "H3-F1")
        self.assertEqual(
            result["parameters"]["entry_condition"], "mean_previous_2_daily_funding_received > 0"
        )
        self.assertEqual(
            result["parameters"]["exit_conditions"],
            [
                "mean_previous_2_daily_funding_received <= 0",
                "date >= far_expiry - 5 calendar days",
            ],
        )
        self.assertEqual(result["hypothesis"], "funding_only_carry")

    def test_exit_conditions_reflect_warmup_observations_for_both_variants(self):
        dates = [f"2026-01-{day:02d}" for day in range(1, 15)]
        carry = _carry_study(dates, basis=(1.0,) * 14, funding=(1.0,) * 14, gross=(1.0,) * 14)
        for warmup in (7, 12):
            basis = self._strategy(
                carry, dates, warmup_observations=warmup, strategy_variant="basis_median"
            )
            funding_only = self._strategy(
                carry, dates, warmup_observations=warmup, strategy_variant="funding_only"
            )
            self.assertEqual(
                basis["parameters"]["exit_conditions"],
                [
                    f"current_basis >= median_previous_{warmup}_basis",
                    f"mean_previous_{warmup}_daily_funding_received <= 0",
                    "date >= far_expiry - 5 calendar days",
                ],
            )
            self.assertEqual(
                funding_only["parameters"]["exit_conditions"],
                [
                    f"mean_previous_{warmup}_daily_funding_received <= 0",
                    "date >= far_expiry - 5 calendar days",
                ],
            )

    def test_retired_study_commands_stay_deleted(self):
        for retired_module in (
            "moex_spread_lab.strategies.funding_carry.funding_screen",
            "moex_spread_lab.strategies.cli_parsers",
        ):
            with self.assertRaises(ModuleNotFoundError):
                importlib.import_module(retired_module)
        parser = argparse.ArgumentParser(prog="moex-spread-lab")
        register_strategy_cli(parser.add_subparsers(dest="command", required=True))
        for retired in ("funding-screen", "carry-study", "carry-strategy", "analytical-baseline"):
            with self.assertRaises(SystemExit), redirect_stderr(StringIO()):
                parser.parse_args([retired])

    def test_normalize_history_rejects_bool_nan_swaprate_and_non_positive_settle(self):
        contracts_path = write_near_far_contracts(
            self.root / "contracts_valid.json",
            near={"secid": "CNYRUBF"},
            far={"secid": "CRZ6", "expiry": "2026-12-17"},
        )
        far_src = source(
            "CRZ6",
            [
                {"TRADEDATE": "2026-02-01", "SETTLEPRICE": 12.2, "SWAPRATE": 0.0},
                {"TRADEDATE": "2026-02-02", "SETTLEPRICE": 12.2, "SWAPRATE": 0.0},
            ],
        )
        for bad_swap in (float("nan"), float("inf"), True, False):
            near_src = source(
                "CNYRUBF",
                [
                    {"TRADEDATE": "2026-02-01", "SETTLEPRICE": 12.0, "SWAPRATE": bad_swap},
                    {"TRADEDATE": "2026-02-02", "SETTLEPRICE": 12.0, "SWAPRATE": 0.01},
                ],
            )
            with self.assertRaises(CarryError):
                build_carry_study(near_src, far_src, contracts_path, near="CNYRUBF", far="CRZ6")

        for bad_settle in (0.0, -5.0, True):
            near_src = source(
                "CNYRUBF",
                [
                    {"TRADEDATE": "2026-02-01", "SETTLEPRICE": bad_settle, "SWAPRATE": 0.01},
                    {"TRADEDATE": "2026-02-02", "SETTLEPRICE": 12.0, "SWAPRATE": 0.01},
                ],
            )
            with self.assertRaises(CarryError):
                build_carry_study(near_src, far_src, contracts_path, near="CNYRUBF", far="CRZ6")

        # Also verify engine signal guard never triggers positive_trailing_funding on NaN funding
        dates = [f"2026-01-{day:02d}" for day in range(1, 7)]
        nan_study = _carry_study(
            dates,
            basis=(1.0,) * 6,
            funding=(float("nan"), -5.0, float("nan"), -5.0, -5.0, -5.0),
            gross=(1.0,) * 6,
        )
        strat = self._strategy(nan_study, dates, strategy_variant="funding_only")
        self.assertEqual(strat["trades"], [])

    def test_funding_carry_plugin_returns_empty_intents_when_lookback_meets_or_exceeds_rows(self):
        contracts_path = write_near_far_contracts(
            self.root / "contracts_lb.json",
            near={"secid": "CNYRUBF"},
            far={"secid": "CRZ6", "expiry": "2026-12-17"},
        )
        bench_path = write_lqdt_lineage(self.root / "bench_lb.json", {"2026-01-01": 1.0})
        dates = [f"2026-02-{d:02d}" for d in range(1, 11)]
        study = build_carry_study(
            source(
                "CNYRUBF",
                [
                    {"TRADEDATE": d, "SETTLEPRICE": 12.0, "SWAPRATE": 0.01, "VOLUME": 500}
                    for d in dates
                ],
            ),
            source(
                "CRZ6",
                [
                    {"TRADEDATE": d, "SETTLEPRICE": 12.2, "SWAPRATE": 0.0, "VOLUME": 500}
                    for d in dates
                ],
            ),
            contracts_path,
            near="CNYRUBF",
            far="CRZ6",
        )
        snap = WindowSnapshot(
            universe="pair_screen",
            pair="CNYRUBF/CRZ6",
            near="CNYRUBF",
            far="CRZ6",
            timeframe="1d",
            carry_study=study,
            benchmark=bench_path,
            extra={"contracts_path": contracts_path, "transaction_cost_scenario": "tariff"},
        )
        self.assertTrue(carry_plugin.check_compatibility(snap).compatible)
        for lb in (10, 20):
            run = carry_plugin.evaluate_intents(snap, {"lookback": lb}, "same_day_settlement")
            self.assertEqual(run.intents, ())

    def test_carry_plugin_and_engine_respect_zero_volume_sessions(self):
        contracts_path = write_near_far_contracts(
            self.root / "contracts_vol.json",
            near={"secid": "CNYRUBF"},
            far={"secid": "CRZ6", "expiry": "2026-12-17"},
        )
        bench_path = write_lqdt_lineage(self.root / "bench_vol.json", {"2026-01-01": 1.0})
        dates = [f"2026-02-{d:02d}" for d in range(1, 8)]
        vols = [500, 0, 0, 0, 0, 0, 0]
        study = build_carry_study(
            source(
                "CNYRUBF",
                [
                    {"TRADEDATE": d, "SETTLEPRICE": 12.0, "SWAPRATE": 0.01, "VOLUME": v}
                    for d, v in zip(dates, vols, strict=True)
                ],
            ),
            source(
                "CRZ6",
                [
                    {"TRADEDATE": d, "SETTLEPRICE": 12.2, "SWAPRATE": 0.0, "VOLUME": v}
                    for d, v in zip(dates, vols, strict=True)
                ],
            ),
            contracts_path,
            near="CNYRUBF",
            far="CRZ6",
        )
        snap = WindowSnapshot(
            universe="pair_screen",
            pair="CNYRUBF/CRZ6",
            near="CNYRUBF",
            far="CRZ6",
            timeframe="1d",
            carry_study=study,
            benchmark=bench_path,
            extra={"contracts_path": contracts_path, "transaction_cost_scenario": "tariff"},
        )
        strict = ResearchSettings(allow_failed_volume=False, allow_failed_capital=True)
        self.assertFalse(carry_plugin.check_compatibility(snap, strict).compatible)
        self.assertEqual(carry_settlement_intents(carry_plugin, snap, {"lookback": 5}), [])

        class _StaticCarryPlugin(FundingCarryPlugin):
            def __init__(self, items: list[dict[str, object]]) -> None:
                self._items = tuple(items)

            def evaluate_intents(self, window, params, fill):
                return StrategyIntentRun(self.strategy_id, fill, dict(params), self._items)

        days = [f"2026-01-0{i}" for i in range(1, 6)]
        rows = [
            {
                "date": day,
                "near_settle": 100 + i,
                "far_settle": 200 + i,
                "near_volume": 100,
                "far_volume": 100,
            }
            for i, day in enumerate(days)
        ]
        trade = {
            "side": "short_spread",
            "entry_date": days[1],
            "exit_date": days[3],
            "signal_date": days[0],
            "reason": "rule_exit",
            "open_at_end": False,
        }
        snap_exp = WindowSnapshot(
            universe="pair_screen",
            pair="N/F",
            near="N",
            far="F",
            carry_study={"rows": [dict(r) for r in rows]},
            contracts={"F": {"expiry": "2026-01-05"}},
        )
        terminal = {
            **trade,
            "exit_date": days[-1],
            "reason": "open_at_data_end_no_executable_exit",
            "open_at_end": True,
        }
        for bad_item in (terminal, trade):
            with self.assertRaises(W0Error):
                carry_settlement_intents(_StaticCarryPlugin([bad_item]), snap_exp, {"lookback": 5})

        future_rows = [dict(r) for r in rows]
        future_rows[2]["funding_known_on"] = "2026-01-09"
        with self.assertRaises(W0Error):
            carry_settlement_intents(
                _StaticCarryPlugin([trade]),
                WindowSnapshot(
                    universe="pair_screen",
                    pair="N/F",
                    near="N",
                    far="F",
                    carry_study={"rows": future_rows},
                ),
                {"lookback": 5},
            )


if __name__ == "__main__":
    unittest.main()
