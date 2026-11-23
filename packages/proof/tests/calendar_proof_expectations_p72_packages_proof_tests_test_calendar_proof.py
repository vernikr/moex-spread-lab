"""Named assertion helpers for the calendar proof tests.

Extracted under P7.2 test-health work (Issue #121) so
``test_calendar_proof`` meets the code-health limits without its size
baseline entry. Each helper re-expresses the assertions that used to be
inline in the test methods; no assertion, expectation or subtest was added,
dropped or weakened.
"""

import unittest
from typing import Any


def assert_spread_relative_to_far_price(testcase: unittest.TestCase, proof: dict[str, Any]) -> None:
    for row in proof["series"]:
        testcase.assertAlmostEqual(row["spread_rel"], row["spread"] / row["far"] * 100, delta=1e-4)
        if row["far_open"]:
            testcase.assertAlmostEqual(
                row["spread_open_rel"], row["spread_open"] / row["far_open"] * 100, delta=1e-4
            )
    for trade in proof["trades"]:
        for side in ("entry", "exit"):
            fill = trade[side]
            far = fill["far_price"]
            testcase.assertAlmostEqual(
                fill["spread_fill_rel"], fill["spread_fill"] / far * 100, delta=1e-4
            )


def assert_zero_volume_day_isolated(
    testcase: unittest.TestCase, proof: dict[str, Any], zero_day: str
) -> None:
    row = next(item for item in proof["series"] if item["date"] == zero_day)
    ledger = next(item for item in proof["ledger"] if item["date"] == zero_day)
    testcase.assertEqual(row["spread_volume"], 0)
    testcase.assertFalse(row["volume_eligible"])
    testcase.assertEqual(ledger["mark_pnl_rub"], 0)
    testcase.assertTrue(
        all(
            trade[side]["date"] != zero_day
            for trade in proof["trades"]
            for side in ("entry", "exit")
        )
    )
    testcase.assertEqual(proof["window"]["min_spread_volume"], 400)


def assert_ledger_reconciliation(
    testcase: unittest.TestCase, proof: dict[str, Any], quantity: int = 2
) -> None:
    testcase.assertGreaterEqual(len(proof["trades"]), 1)
    testcase.assertEqual(proof["summary"]["ledger_reconciliation_rub"], 0.0)
    testcase.assertEqual(proof["summary"]["fx_point_value_drift_rub"], 0.0)
    first = proof["trades"][0]
    testcase.assertEqual(first["quantity"], quantity)
    testcase.assertGreater(first["entry"]["costs"]["exchange_clearing"], 0)
    testcase.assertEqual(first["entry"]["spread_volume"], 400)
    testcase.assertEqual(first["net_rub"], round(first["gross_rub"] - first["costs_rub"], 2))
    testcase.assertEqual(len(proof["series"]), len(proof["ledger"]))
    testcase.assertAlmostEqual(
        proof["ledger"][-1]["cumulative_net_rub"], proof["summary"]["net_rub"], places=1
    )


def assert_h1_proxy_window(testcase: unittest.TestCase, proof: dict[str, Any]) -> None:
    window = proof["window"]
    testcase.assertEqual(window["price_source"], "synthetic_leg_proxy")
    testcase.assertEqual(window["native_fallback_reason"], "no_local_h1_file")
    testcase.assertFalse(window["has_native_spread_volume"])
    testcase.assertEqual(window["liquidity_source"], "matched_near_far_session_volume")
    testcase.assertEqual(window["liquidity_gate_unit"], "контр./сессию")
    testcase.assertEqual(window["min_liquidity_volume"], 240)
    testcase.assertTrue(window["tradable_every_session"])
    testcase.assertEqual(len(proof["series"]), 36)
    testcase.assertEqual(len({row["timestamp"] for row in proof["series"]}), 36)
    testcase.assertTrue(all(row["spread_volume"] is None for row in proof["series"]))
    testcase.assertEqual(proof["series"][0]["ruonia_rate_date"], "2022-01-01")
    testcase.assertEqual(proof["series"][0]["benchmark_mark_date"], "2022-12-12")
    testcase.assertTrue(any(not row["signal_eligible"] for row in proof["series"]))
    testcase.assertTrue(any(row["signal_eligible"] for row in proof["series"]))
    # Daily mark rounding can differ from the per-trade rounded sum by one kopeck.
    testcase.assertLessEqual(abs(proof["summary"]["ledger_reconciliation_rub"]), 0.01)


def assert_h1_convergence_to_ruonia(
    testcase: unittest.TestCase, convergence: dict[str, Any]
) -> None:
    testcase.assertEqual(convergence["selection"]["strategy"], "convergence_to_ruonia")
    testcase.assertEqual(convergence["series"][0]["ruonia_rate_date"], "2022-01-01")
    testcase.assertTrue(all(row["dev_pp"] is not None for row in convergence["series"]))


def assert_instrument_provenance(testcase: unittest.TestCase, proof: dict[str, Any]) -> None:
    testcase.assertEqual(proof["instrument"]["near"], "SRH3")
    testcase.assertEqual(proof["instrument"]["far"], "SRM3")
    testcase.assertEqual(proof["instrument"]["spread_instrument"], "SRH3SRM3")
    testcase.assertEqual(proof["instrument"]["near_expiry"], "2023-03-16")
    provenance = proof["provenance"]
    for key in (
        "near_history_sha256",
        "far_history_sha256",
        "spread_history_sha256",
        "contracts_sha256",
        "benchmark_sha256",
        "ruonia_sha256",
    ):
        testcase.assertTrue(provenance.get(key), key)
    testcase.assertEqual(len(proof["run_id"]), 16)
    testcase.assertIn("календарн", proof["price_role"].lower())
