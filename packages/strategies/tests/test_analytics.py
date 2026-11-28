import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.accounting.costs import side_cost, tariff_profile
from moex_spread_lab.strategies.pair_mean_reversion.engine import (
    build_daily_series,
    build_series,
    get_cost_scenario,
    load_contracts,
    run_mean_reversion_baseline,
)
from strategy_fixtures import write_lqdt_lineage, write_near_far_contracts


class HistoricalTariffGroupTests(unittest.TestCase):
    def test_historical_contract_tariff_group_reaches_cost_calculation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = {
                "instruments": [
                    {
                        "secid": "PERP",
                        "lot_size": 1,
                        "tick_size": 1,
                        "tick_value": 1,
                        "initial_margin": 100,
                        "source_vintage": "current",
                        "tariff_group": "interest",
                    },
                ]
            }
            historical = {
                "instruments": [
                    {
                        "secid": "OLDQ",
                        "lot_size": 1,
                        "tick_size": 1,
                        "tick_value": 1,
                        "initial_margin": 100,
                        "source_vintage": "assumed",
                        "tariff_group": "interest",
                    },
                ]
            }
            (root / "contracts.json").write_text(json.dumps(current))
            (root / "historical-contracts.json").write_text(json.dumps(historical))
            terms, _ = load_contracts(root / "contracts.json", "PERP", "OLDQ")
            self.assertEqual(terms["OLDQ"]["tariff_group"], "interest")
            result = side_cost(tariff_profile(), [(terms["OLDQ"], 100)], 1, as_of="2026-11-01")
            self.assertGreater(result["exchange_clearing"], 0)


def candle_payload(secid, closes, interval=24):
    return {
        "schema_version": "iss-candles.v1",
        "secid": secid,
        "interval": interval,
        "rows": [
            {
                "begin": (
                    f"2026-01-{index // 2 + 1:02d} {index % 2 * 10:02d}:00:00"
                    if interval == 10
                    else f"2026-01-{index + 1:02d} 00:00:00"
                ),
                "close": close,
                "price_proxy_kind": "candle_close",
                "source": {"raw_response_sha256": f"{secid}-{index}"},
            }
            for index, close in enumerate(closes)
        ],
    }


SPREADS = (0, 10, 20, 30, 20, 10, 0, -10, -20, -10, 0, 10, 20, 10, 0)


def spread_rows(*, timestamps=False):
    """Daily synthetic spread rows around a flat 10.0 far leg and an observed flat LQDT mark."""
    rows = []
    for index, spread in enumerate(SPREADS):
        date = f"2026-01-{index + 1:02d}"
        row: dict[str, object] = {"date": date}
        if timestamps:
            row["timestamp"] = f"{date} 00:00:00"
        row.update(
            near_close=10.0 + spread / 1000,
            far_close=10.0,
            spread_ticks=spread,
            benchmark_price=1.0,
            benchmark_source_kind="observed",
        )
        rows.append(row)
    return rows


def contract_terms():
    """In-memory NEAR/FAR contract terms matching the default synthetic snapshot."""
    return {
        "NEAR": {"lot_size": 1000, "tick_size": 0.001, "tick_value": 1.0, "initial_margin": 900},
        "FAR": {"lot_size": 1000, "tick_size": 0.001, "tick_value": 1.0, "initial_margin": 1000},
    }


class AnalyticsTests(unittest.TestCase):
    def test_daily_series_uses_tick_safe_spread_and_benchmark(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "near.json").write_text(
                json.dumps(candle_payload("NEAR", [10.0, 10.1])), encoding="utf-8"
            )
            (root / "far.json").write_text(
                json.dumps(candle_payload("FAR", [9.9, 9.95])), encoding="utf-8"
            )
            write_lqdt_lineage(root / "benchmark.json", {"2026-01-01": 1.0, "2026-01-02": 1.01})
            write_near_far_contracts(
                root / "contracts.json", near={"expiry": "2026-12-01"}, far={"expiry": "2027-03-01"}
            )
            series = build_daily_series(
                root / "near.json",
                root / "far.json",
                root / "benchmark.json",
                root / "contracts.json",
                near="NEAR",
                far="FAR",
            )

        self.assertEqual(series["rows"][0]["spread_ticks"], 100.0)
        self.assertEqual(series["rows"][1]["spread_ticks"], 150.0)
        self.assertEqual(series["rows"][1]["benchmark_source_kind"], "observed")

    def test_baseline_reports_gross_result_and_unknown_costs(self):
        rows = spread_rows()
        result = run_mean_reversion_baseline(
            {
                "near": "NEAR",
                "far": "FAR",
                "contracts": contract_terms(),
                "rows": rows,
                "provenance": {},
            },
            lookback=3,
            entry_z=1.0,
            exit_z=0.5,
        )

        self.assertEqual(result["costs"]["status"], "unknown")
        self.assertGreaterEqual(result["summary"]["trade_count"], 1)
        self.assertEqual(result["release_state"], "exploratory_inconclusive")
        self.assertEqual(result["daily"][0]["X"], 0.0)

    def test_intraday_marks_use_previous_daily_close_until_last_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "near.json").write_text(
                json.dumps(candle_payload("NEAR", [10.0, 10.1, 10.2], interval=10)),
                encoding="utf-8",
            )
            (root / "far.json").write_text(
                json.dumps(candle_payload("FAR", [9.9, 9.95, 10.0], interval=10)), encoding="utf-8"
            )
            write_lqdt_lineage(
                root / "benchmark.json", {"2025-12-31": 1.0, "2026-01-01": 1.01, "2026-01-02": 1.02}
            )
            write_near_far_contracts(
                root / "contracts.json", near={"expiry": "2026-12-01"}, far={"expiry": "2027-03-01"}
            )
            series = build_series(
                root / "near.json",
                root / "far.json",
                root / "benchmark.json",
                root / "contracts.json",
                near="NEAR",
                far="FAR",
                interval=10,
            )

        self.assertEqual(series["rows"][0]["benchmark_mark_kind"], "prior_daily_close")
        self.assertEqual(series["rows"][-1]["benchmark_mark_kind"], "daily_close_same_date")

    def test_cost_profile_keeps_components_separate_and_reduces_net_value(self):
        rows = spread_rows()
        series = {
            "near": "NEAR",
            "far": "FAR",
            "contracts": contract_terms(),
            "rows": rows,
            "provenance": {},
        }
        result = run_mean_reversion_baseline(
            series,
            lookback=3,
            entry_z=1.0,
            exit_z=0.5,
            cost_profile=get_cost_scenario("base"),
        )

        self.assertEqual(result["costs"]["status"], "illustrative_unverified")
        self.assertGreater(result["summary"]["modeled_costs"], 0)
        self.assertGreater(result["costs"]["total_by_component"]["slippage"], 0)
        self.assertTrue(all(trade["net_pnl"] is not None for trade in result["trades"]))

    def test_baseline_report_keeps_scheduler_contract(self):
        report = run_mean_reversion_baseline(
            {
                "near": "NEAR",
                "far": "FAR",
                "timeframe": "1d",
                "contracts": contract_terms(),
                "rows": spread_rows(timestamps=True),
                "provenance": {},
            },
            initial_capital=30000,
            lookback=3,
            entry_z=1.0,
            exit_z=0.5,
            quantity=1,
            decision_delay_bars=1,
            analysis_role="primary_baseline",
        )
        self.assertEqual(report["schema_version"], "analytical-baseline.v1")
        self.assertGreater(report["summary"]["trade_count"], 0)
        self.assertIn("final_X", report["summary"])
        self.assertTrue(report["trades"])


if __name__ == "__main__":
    unittest.main()
