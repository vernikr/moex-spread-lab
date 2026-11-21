import unittest

from moex_spread_lab.analytics import run_mean_reversion_baseline
from moex_spread_lab.costs import side_cost, tariff_profile
from moex_spread_lab.portfolio import lqdt_overlay_values
from moex_spread_lab.w0 import W0Error

CONTRACT = {"secid": "SBERF", "lot_size": 100, "tick_value": 1.0, "tick_size": 0.01, "initial_margin": 1000.0}


def _series(spike_close=15.0, next_open=14.0):
    closes = [10.0 + 0.1 * (i % 2) for i in range(25)]
    closes[22] = spike_close
    opens = list(closes)
    opens[23] = next_open
    rows = [{
        "date": f"2026-03-{day:02d}", "timestamp": f"2026-03-{day:02d} 00:00:00",
        "near_close": 10.0, "far_close": closes[i], "near_open": 10.0, "far_open": opens[i],
        "spread_ticks": 10.0 - closes[i], "benchmark_price": 1.0, "benchmark_source_kind": "observed",
    } for i, day in enumerate(range(1, 26))]
    contracts = {
        "SBERF": {**CONTRACT, "tick_size": 1.0},
        "SRZ6": {**CONTRACT, "secid": "SRZ6", "tick_size": 1.0},
    }
    return {"rows": rows, "contracts": contracts, "near": "SBERF", "far": "SRZ6", "provenance": {}}


class CostTests(unittest.TestCase):
    def test_tariff_uses_group_rate_on_contract_value_and_slippage_ticks(self):
        cost = side_cost(tariff_profile(2.0), [(CONTRACT, 300.0)], 3)
        self.assertAlmostEqual(cost["exchange_clearing"], 300 * 100 * 0.0003 * 3)
        self.assertEqual(cost["slippage"], 6.0)
        self.assertEqual(cost["broker"], 0.0)

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


class OpenFillTests(unittest.TestCase):
    def test_open_fill_uses_the_next_bar_open_not_the_signal_close(self):
        common = {"lookback": 20, "entry_z": 2.0, "exit_z": 0.5, "analysis_role": "diagnostic_only"}
        by_close = run_mean_reversion_baseline(_series(), **common)
        by_open = run_mean_reversion_baseline(_series(), fill="open", **common)
        self.assertAlmostEqual(by_close["trades"][0]["entry_spread_ticks"], 10.0 - 10.1)
        self.assertEqual(by_open["trades"][0]["entry_spread_ticks"], 10.0 - 14.0)

    def test_tariff_costs_charge_both_sides_and_overlay_keeps_cash_in_lqdt(self):
        report = run_mean_reversion_baseline(
            _series(), fill="open", cash_in_lqdt=True, cost_profile={**tariff_profile(1.0)},
            analysis_role="diagnostic_only", initial_capital=30000.0,
        )
        costs = report["costs"]["total_by_component"]
        self.assertGreater(costs["slippage"], 0)
        self.assertEqual(report["parameters"]["accounting"], "free_cash_in_lqdt_margin_cash_idle")
        self.assertEqual(report["summary"]["cash_shortfall_rows"], 0)


if __name__ == "__main__":
    unittest.main()
