"""Exact signal boundaries, explicit same-bar reference and compact table results.

Shared requests and service scaffolding live in
``proof_windows_fixtures_p72_packages_proof_tests_test_proof_windows``; the
detailed band, fill and compact-row expectations live in
``proof_windows_expectations_p72_packages_proof_tests_test_proof_windows``.
"""

import json
import math
import statistics
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

from moex_spread_lab.core.settings import save_settings, settings_path
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.proof_search import StudyWindow, research_run
from moex_spread_lab.proof.proof_windows import MAX_WINDOW_REQUESTS, window_summaries
from moex_spread_lab.proof.service import ProofService
from moex_spread_lab.strategies.pair_mean_reversion.analytics import (
    AnalyticsError,
    run_mean_reversion_baseline,
)
from moex_spread_lab.strategies.signals import attach_entry_levels, pair_fill
from proof_fixtures import CAL, FLAT, PAIR, two_calendar_windows

# ---- folded from the retired P7.2 shard proof_windows_fixtures_p72_packages_proof_tests_test_proof_windows (PAIR/CAL/FLAT now in proof_fixtures) ----

FILLS = ("next_open", "next_close", "reference_same_bar")


def group_request(fill: str) -> dict[str, Any]:
    """Pair request widened to a checked cross-universe group for the given fill."""
    return {
        **PAIR,
        "fill": fill,
        "targets": [
            {k: v for k, v in r.items() if k in ("universe", "asset", "pair")} for r in (PAIR, CAL)
        ],
    }


class FixtureCase(unittest.TestCase):
    """Temporary proof service seeded with the two-calendar-window fixture."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        two_calendar_windows(self.root)
        self.service = ProofService(self.root)

    def run_proof(self, req):
        return research_run(self.service, req, optimize=False, include_plateau=False)


# ---- folded from the retired P7.2 shard proof_windows_expectations_p72_packages_proof_tests_test_proof_windows ----
def band_levels(
    base: dict[str, Any], fill: str, series: list[dict[str, Any]]
) -> tuple[dict[str, Any], float, float]:
    """Return the first full-window row and the entry/exit bands it must carry.

    The pair universe prices a RUB-neutral spread, so it uses the population
    deviation of the raw spread; the calendar universe prices a RUB point value,
    so it uses the sample deviation of the RUB-scaled spread. ``reference_same_bar``
    consumes one extra leading row because entry happens on the signal bar.
    """
    native = base["universe"] == "calendar_native"
    rows = [r for r in series if r.get("volume_eligible", True)]
    window = base["parameters"]["window" if native else "lookback"]
    i = window + 1
    end = i + 1 if native and fill == "reference_same_bar" else i
    sample = [
        r["spread"] * (r["point_value_rub"] if native else 1) for r in rows[end - window : end]
    ]
    mean = statistics.mean(sample)
    deviation = statistics.stdev(sample) if native else statistics.pstdev(sample)
    point = rows[i]["point_value_rub"] if native else 1
    entry_z = base["parameters"]["entry_z"]
    return rows[i], (mean - entry_z * deviation) / point, (mean + entry_z * deviation) / point


def assert_fill_selection(case: FixtureCase, proof: dict[str, Any], fill: str) -> None:
    """The selected fill drives execution reference, replay identity and entry prices."""
    same_bar = fill == "reference_same_bar"
    case.assertEqual(proof["selection"]["fill"], fill)
    case.assertEqual(proof["execution_reference"], same_bar)
    case.assertEqual(proof["evaluation"]["execution_reference"], same_bar)
    case.assertEqual(proof["summary"]["ledger_reconciliation_rub"], 0)
    case.assertEqual(case.run_proof(proof["selection"])["run_id"], proof["run_id"])
    case.assertTrue(proof["trades"])
    rows = {r["date"]: r for r in proof["series"]}
    for trade in proof["trades"]:
        row = rows[trade["entry"]["date"]]
        case.assertEqual(
            trade["entry"]["near_price"],
            row["near_open" if fill == "next_open" else "near"],
        )
        if same_bar:
            case.assertEqual(trade["signal"]["date"], trade["entry"]["date"])
    if same_bar:
        case.assertTrue(any("не является проверкой исполнимой" in note for note in proof["notes"]))


def assert_entry_matches_signal_band(
    case: FixtureCase, trade: dict[str, Any], signal: dict[str, Any]
) -> None:
    """A long entry triggers at or below the buy band, a short one at or above the sell."""
    long_side = trade["side"] == "long_spread"
    bound = signal["buy_level" if long_side else "sell_level"]
    case.assertIsNotNone(bound)
    if long_side:
        case.assertLessEqual(signal["spread"] - bound, 1e-6)
    else:
        case.assertGreaterEqual(signal["spread"] - bound, -1e-6)


def assert_zero_volume_row_is_ignored(case: FixtureCase, proof: dict[str, Any]) -> None:
    """A zero-volume row cannot poison later bands, even with an absurd spread."""
    altered = deepcopy(proof)
    altered["series"][0]["volume_eligible"] = False
    altered["series"][0]["spread"] = 1e10
    attach_entry_levels(altered)
    without = deepcopy(proof)
    without["series"] = without["series"][1:]
    attach_entry_levels(without)
    case.assertEqual(
        [(r["buy_level"], r["sell_level"]) for r in altered["series"]][1:],
        [(r["buy_level"], r["sell_level"]) for r in without["series"]],
    )


def assert_bands_are_prefix_causal(case: FixtureCase, proof: dict[str, Any]) -> None:
    """Bands recomputed on a truncated series reproduce the full-series prefix."""
    prefix = deepcopy(proof)
    prefix["series"] = prefix["series"][:30]
    attach_entry_levels(prefix)
    case.assertEqual(
        [(r["buy_level"], r["sell_level"]) for r in proof["series"]][:30],
        [(r["buy_level"], r["sell_level"]) for r in prefix["series"]],
    )


def assert_bands_follow_point_value(case: FixtureCase, proof: dict[str, Any]) -> None:
    """Changing the RUB point value moves the bands, so it is not ignored."""
    fx = deepcopy(proof)
    for i, row in enumerate(fx["series"]):
        row["point_value_rub"] = 2 + i / 10
    attach_entry_levels(fx)
    case.assertNotEqual(fx["series"][20]["buy_level"], proof["series"][20]["buy_level"])


def assert_compact_row_matches_proof(
    case: FixtureCase, row: dict[str, Any], proof: dict[str, Any]
) -> None:
    """A compact window row is the exact chart result with the heavy payloads omitted."""
    case.assertEqual(row["status"], "measured")
    case.assertEqual(row["run_id"], proof["run_id"])
    case.assertEqual(row["trades"], proof["summary"]["trades"])
    case.assertEqual(
        row["annualized_excess_pp"],
        proof["evaluation"]["result"].get("annualized_excess_pp"),
    )
    case.assertEqual(row["evaluation"], proof["evaluation"])
    case.assertNotIn("ledger", row)
    case.assertNotIn("plateau", row)


class ReferenceAndBandsTests(FixtureCase):
    def test_same_bar_and_next_close_work_in_both_universes_and_replay_identically(
        self,
    ):
        for base in (PAIR, CAL):
            ids = []
            for fill in FILLS:
                with self.subTest(universe=base["universe"], fill=fill):
                    p = self.run_proof({**base, "fill": fill})
                    ids.append(p["run_id"])
                    assert_fill_selection(self, p, fill)
            self.assertEqual(len(set(ids)), 3)
        self.assertEqual(
            self.run_proof(PAIR)["run_id"],
            self.run_proof({**PAIR, "fill": "reference_same_bar"})["run_id"],
        )

    def test_every_entry_agrees_with_its_signal_date_band_not_the_later_fill(self):
        for base in (PAIR, CAL):
            for fill in FILLS:
                with self.subTest(base=base["universe"], fill=fill):
                    p = self.run_proof({**base, "fill": fill})
                    rows = {r["date"]: r for r in p["series"]}
                    for t in p["trades"]:
                        assert_entry_matches_signal_band(self, t, rows[t["signal"]["date"]])
                        if fill != "reference_same_bar":
                            self.assertLess(t["signal"]["date"], t["entry"]["date"])

    def test_pair_uses_population_deviation_and_calendar_uses_rub_sample_deviation(
        self,
    ):
        for base in (PAIR, CAL):
            for fill in ("next_open", "reference_same_bar"):
                with self.subTest(universe=base["universe"], fill=fill):
                    p = self.run_proof({**base, "fill": fill})
                    row, buy, sell = band_levels(base, fill, p["series"])
                    self.assertAlmostEqual(row["buy_level"], buy, places=7)
                    self.assertAlmostEqual(row["sell_level"], sell, places=7)

    def test_bands_are_prefix_causal_and_zero_volume_poison_cannot_enter_the_window(
        self,
    ):
        p = self.run_proof(CAL)
        assert_zero_volume_row_is_ignored(self, p)
        assert_bands_are_prefix_causal(self, p)
        assert_bands_follow_point_value(self, p)

    def test_ruonia_lines_invert_exactly_to_the_two_rate_thresholds(self):
        p = self.run_proof(
            {
                **CAL,
                "strategy": "convergence_to_ruonia",
                "parameters": {"threshold_pp": 0.7},
            }
        )
        for r in p["series"]:
            if r.get("buy_level") is None:
                continue
            self.assertAlmostEqual(
                r["dev_pp"] + (r["buy_level"] - r["spread"]) / r["rate_price_per_pp"],
                -0.7,
            )
            self.assertAlmostEqual(
                r["dev_pp"] + (r["sell_level"] - r["spread"]) / r["rate_price_per_pp"],
                0.7,
            )
            self.assertAlmostEqual(r["buy_level_rel"], r["buy_level"] / r["far"] * 100)

    def test_funding_has_no_fabricated_price_bands_and_does_not_accept_a_price_fill(
        self,
    ):
        req = {**PAIR, "strategy": "funding_only", "parameters": {"lookback": 8}}
        p = self.run_proof(req)
        self.assertEqual(p["signal_levels"]["kind"], "not_price_based")
        self.assertFalse(any(r.get("buy_level") is not None for r in p["series"]))
        for bad in ("reference_same_bar", "next_close", "invalid", None):
            with self.subTest(fill=bad), self.assertRaises(W0Error):
                pair_fill({"fill": bad}, "funding_only")
        with self.assertRaises(W0Error):
            self.run_proof({**PAIR, "fill": "invalid"})

    def test_primary_analytics_still_forbid_same_bar_execution(self):
        # Validation must happen before any market data is consumed.
        with self.assertRaises(AnalyticsError):
            run_mean_reversion_baseline({}, decision_delay_bars=0)
        with self.assertRaises(AnalyticsError):
            run_mean_reversion_baseline(
                {}, decision_delay_bars=0, analysis_role="diagnostic_only", fill="open"
            )

    def test_checked_pair_and_calendar_no_longer_reject_common_close_fills(self):
        for fill in ("next_close", "reference_same_bar"):
            result = research_run(self.service, group_request(fill), optimize=True)
            self.assertEqual(result["status"], "maximum_found")
            self.assertEqual(result["group"]["member_count"], 2)
            self.assertEqual(result["group"]["execution_reference"], fill == "reference_same_bar")
            self.assertTrue(all(m["selection"]["fill"] == fill for m in result["group"]["members"]))
            self.assertTrue(
                all(
                    m["selection"]["execution_reference"] == (fill == "reference_same_bar")
                    for m in result["group"]["members"]
                )
            )


class WindowSummaryTests(FixtureCase):
    def test_every_compact_result_matches_the_exact_chart_and_does_not_mutate_requests(
        self,
    ):
        requests = [PAIR, CAL, FLAT]
        original = deepcopy(requests)
        for share in (0, 25):
            save_settings(self.root, {"validation_percent": share, "slippage_ticks": 2.5})
            result = window_summaries(self.service, {"requests": requests})
            self.assertEqual(result["validation_percent"], share)
            for req, row in zip(requests, result["rows"], strict=True):
                assert_compact_row_matches_proof(self, row, self.run_proof(req))
            self.assertEqual(result["rows"][-1]["trades"], 0)
            self.assertEqual(result["rows"][-1]["annualized_excess_pp"], 0)
        self.assertEqual(requests, original)
        self.assertFalse((self.root / "data/analytics/proof-searches.jsonl").exists())

    def test_unavailable_row_is_not_a_zero_and_never_removes_another_row(self):
        bad = {**PAIR, "pair": "BTCUSDF/BTZ6"}
        invalid = {**CAL, "parameters": {"window": None}}
        result = window_summaries(self.service, {"requests": [PAIR, bad, invalid]})
        self.assertEqual(result["rows"][0]["status"], "measured")
        for row in result["rows"][1:]:
            self.assertEqual(row["status"], "unavailable")
            self.assertIsNone(row["trades"])
            self.assertIsNone(row["annualized_excess_pp"])
            self.assertTrue(row["reason"])

    def test_accepted_trade_count_does_not_count_unfunded_intents(self):
        path = self.root / "data/w1/current-intake.json"
        data = json.loads(path.read_text())
        for contract in data["instruments"]:
            if contract["secid"] in ("CNYRUBF", "CRZ6"):
                contract["initial_margin"] = 16000
        path.write_text(json.dumps(data))
        row = window_summaries(self.service, {"requests": [PAIR]})["rows"][0]
        self.assertEqual(row["trades"], 0)
        self.assertEqual(row["annualized_excess_pp"], 0)

    def test_all_rows_share_one_settings_snapshot_and_cancellation_is_observed(self):
        def changed(*args):
            save_settings(self.root, {"slippage_ticks": 4})
            return StudyWindow(*args)

        with patch("moex_spread_lab.proof.proof_windows.StudyWindow", side_effect=changed):
            result = window_summaries(self.service, {"requests": [PAIR, CAL]})
        self.assertEqual(result["slippage_ticks"], 1)
        self.assertTrue(all(r["selection"]["slippage_ticks"] == 1 for r in result["rows"]))
        with self.assertRaises(ConnectionAbortedError):
            window_summaries(
                self.service,
                {"requests": [PAIR, CAL]},
                check_cancelled=lambda: (_ for _ in ()).throw(ConnectionAbortedError()),
            )

    def test_body_limits_identity_validation_and_catalog_checks(self):
        for request in (
            None,
            {},
            {"requests": []},
            {"requests": [PAIR] * (MAX_WINDOW_REQUESTS + 1)},
            {"requests": [PAIR, PAIR]},
            {"requests": [{"pair": None}]},
            {"requests": [PAIR], "targets": []},
        ):
            with self.subTest(request=request), self.assertRaises(W0Error):
                window_summaries(self.service, request)
        catalog = self.service.catalog()
        pair = next(p for p in catalog["pair_screen"]["pairs"] if p["id"] == PAIR["pair"])
        proof = self.run_proof(PAIR)
        self.assertEqual(pair["passes_capital_screen"], proof["window"]["passes_capital_screen"])
        self.assertEqual(pair["tradable_every_day"], proof["window"]["tradable_every_day"])
        self.assertTrue(math.isfinite(pair["max_pair_margin_rub"]))

    def test_window_summaries_is_read_only_and_returns_requested_rows(self):
        result = window_summaries(self.service, {"requests": [PAIR, CAL]})
        self.assertEqual(len(result["rows"]), 2)
        self.assertFalse(settings_path(self.root).exists())
