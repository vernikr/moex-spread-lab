"""Exact signal boundaries, explicit same-bar reference and compact table results.

Shared requests and service scaffolding live in
``proof_windows_fixtures_p72_packages_proof_tests_test_proof_windows``; the
detailed band, fill and compact-row expectations live in
``proof_windows_expectations_p72_packages_proof_tests_test_proof_windows``.
"""

import http.client
import json
import math
import threading
from copy import deepcopy
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from moex_spread_lab.core.settings import save_settings, settings_path
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime.proof_search import StudyWindow, research_run
from moex_spread_lab.proof_runtime.proof_windows import MAX_WINDOW_REQUESTS, window_summaries
from moex_spread_lab.strategies.pair_mean_reversion.analytics import (
    AnalyticsError,
    run_mean_reversion_baseline,
)
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs
from moex_spread_lab.strategies.signals import pair_fill
from moex_spread_lab.web.server import ScreenerStore, make_handler
from proof_windows_expectations_p72_packages_proof_tests_test_proof_windows import (
    assert_bands_are_prefix_causal,
    assert_bands_follow_point_value,
    assert_compact_row_matches_proof,
    assert_entry_matches_signal_band,
    assert_fill_selection,
    assert_zero_volume_row_is_ignored,
    band_levels,
)
from proof_windows_fixtures_p72_packages_proof_tests_test_proof_windows import (
    CAL,
    FILLS,
    FLAT,
    PAIR,
    FixtureCase,
    group_request,
)


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

        with patch("moex_spread_lab.proof_runtime.proof_windows.StudyWindow", side_effect=changed):
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

    def test_http_table_endpoint_is_read_only_and_local_origin_only(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(self.root)))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        self.addCleanup(conn.close)
        headers = {"Content-Type": "application/json"}
        body = json.dumps({"requests": [PAIR, CAL]})
        conn.request("POST", "/api/proof/windows", body, headers)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(len(json.loads(response.read())["rows"]), 2)
        self.assertFalse(settings_path(self.root).exists())
        conn.request(
            "POST",
            "/api/proof/windows",
            body,
            {**headers, "Origin": "https://example.invalid"},
        )
        response = conn.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
