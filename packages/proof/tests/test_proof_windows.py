"""Exact signal boundaries, explicit same-bar reference and compact table results."""

import http.client
import json
import math
import statistics
import tempfile
import threading
import unittest
from copy import deepcopy
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from proof_fixtures import two_calendar_windows

from moex_spread_lab.analytics import AnalyticsError, run_mean_reversion_baseline
from moex_spread_lab.evaluation_periods import save_settings, settings_path
from moex_spread_lab.proof import ProofService
from moex_spread_lab.proof_search import StudyWindow, research_run
from moex_spread_lab.proof_signals import attach_entry_levels, pair_fill
from moex_spread_lab.proof_windows import MAX_WINDOW_REQUESTS, window_summaries
from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler
from moex_spread_lab.w0 import W0Error

PAIR = {
    "universe": "pair_screen",
    "pair": "CNYRUBF/CRZ6",
    "strategy": "mean_reversion",
    "parameters": {"lookback": 6, "entry_z": 0.8, "exit_z": 0.1},
    "sizing": "max_affordable",
}
CAL = {
    "universe": "calendar_native",
    "asset": "SR",
    "pair": "SRH3/SRM3",
    "strategy": "zscore_mean_reversion",
    "parameters": {"window": 10, "entry_z": 1.5, "exit_z": 0.5},
    "sizing": "max_affordable",
}
FLAT = {**CAL, "pair": "SRM3/SRU3"}


class FixtureCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        two_calendar_windows(self.root)
        self.service = ProofService(self.root)

    def run_proof(self, req):
        return research_run(self.service, req, optimize=False, include_plateau=False)


class ReferenceAndBandsTests(FixtureCase):
    def test_same_bar_and_next_close_work_in_both_universes_and_replay_identically(
        self,
    ):
        for base in (PAIR, CAL):
            ids = []
            for fill in ("next_open", "next_close", "reference_same_bar"):
                with self.subTest(universe=base["universe"], fill=fill):
                    p = self.run_proof({**base, "fill": fill})
                    ids.append(p["run_id"])
                    self.assertEqual(p["selection"]["fill"], fill)
                    self.assertEqual(
                        p["execution_reference"], fill == "reference_same_bar"
                    )
                    self.assertEqual(
                        p["evaluation"]["execution_reference"],
                        fill == "reference_same_bar",
                    )
                    self.assertEqual(p["summary"]["ledger_reconciliation_rub"], 0)
                    self.assertEqual(
                        self.run_proof(p["selection"])["run_id"], p["run_id"]
                    )
                    self.assertTrue(p["trades"])
                    rows = {r["date"]: r for r in p["series"]}
                    for trade in p["trades"]:
                        row = rows[trade["entry"]["date"]]
                        self.assertEqual(
                            trade["entry"]["near_price"],
                            row["near_open" if fill == "next_open" else "near"],
                        )
                        if fill == "reference_same_bar":
                            self.assertEqual(
                                trade["signal"]["date"], trade["entry"]["date"]
                            )
                    if fill == "reference_same_bar":
                        self.assertTrue(
                            any(
                                "не является проверкой исполнимой" in n
                                for n in p["notes"]
                            )
                        )
            self.assertEqual(len(set(ids)), 3)
        self.assertEqual(
            self.run_proof(PAIR)["run_id"],
            self.run_proof({**PAIR, "fill": "reference_same_bar"})["run_id"],
        )

    def test_every_entry_agrees_with_its_signal_date_band_not_the_later_fill(self):
        for base in (PAIR, CAL):
            for fill in ("next_open", "next_close", "reference_same_bar"):
                with self.subTest(base=base["universe"], fill=fill):
                    p = self.run_proof({**base, "fill": fill})
                    rows = {r["date"]: r for r in p["series"]}
                    for t in p["trades"]:
                        signal = rows[t["signal"]["date"]]
                        bound = signal[
                            "buy_level" if t["side"] == "long_spread" else "sell_level"
                        ]
                        self.assertIsNotNone(bound)
                        self.assertLessEqual(signal["spread"] - bound, 1e-6) if t[
                            "side"
                        ] == "long_spread" else self.assertGreaterEqual(
                            signal["spread"] - bound, -1e-6
                        )
                        if fill != "reference_same_bar":
                            self.assertLess(t["signal"]["date"], t["entry"]["date"])

    def test_pair_uses_population_deviation_and_calendar_uses_rub_sample_deviation(
        self,
    ):
        for base in (PAIR, CAL):
            for fill in ("next_open", "reference_same_bar"):
                with self.subTest(universe=base["universe"], fill=fill):
                    p = self.run_proof({**base, "fill": fill})
                    native = base["universe"] == "calendar_native"
                    rows = [r for r in p["series"] if r.get("volume_eligible", True)]
                    window = base["parameters"]["window" if native else "lookback"]
                    i = window + 1
                    end = i + 1 if native and fill == "reference_same_bar" else i
                    sample = [
                        r["spread"] * (r["point_value_rub"] if native else 1)
                        for r in rows[end - window : end]
                    ]
                    mean = statistics.mean(sample)
                    deviation = (
                        statistics.stdev(sample) if native else statistics.pstdev(sample)
                    )
                    point = rows[i]["point_value_rub"] if native else 1
                    self.assertAlmostEqual(
                        rows[i]["buy_level"],
                        (mean - base["parameters"]["entry_z"] * deviation) / point,
                        places=7,
                    )
                    self.assertAlmostEqual(
                        rows[i]["sell_level"],
                        (mean + base["parameters"]["entry_z"] * deviation) / point,
                        places=7,
                    )

    def test_bands_are_prefix_causal_and_zero_volume_poison_cannot_enter_the_window(
        self,
    ):
        p = self.run_proof(CAL)
        original = deepcopy(p)
        altered = deepcopy(p)
        altered["series"][0]["volume_eligible"] = False
        altered["series"][0]["spread"] = 1e10
        attach_entry_levels(altered)
        without = deepcopy(p)
        without["series"] = without["series"][1:]
        attach_entry_levels(without)
        self.assertEqual(
            [(r["buy_level"], r["sell_level"]) for r in altered["series"]][1:],
            [(r["buy_level"], r["sell_level"]) for r in without["series"]],
        )
        prefix = deepcopy(original)
        prefix["series"] = prefix["series"][:30]
        attach_entry_levels(prefix)
        self.assertEqual(
            [(r["buy_level"], r["sell_level"]) for r in original["series"]][:30],
            [(r["buy_level"], r["sell_level"]) for r in prefix["series"]],
        )
        fx = deepcopy(p)
        for i, row in enumerate(fx["series"]):
            row["point_value_rub"] = 2 + i / 10
        attach_entry_levels(fx)
        self.assertNotEqual(fx["series"][20]["buy_level"], p["series"][20]["buy_level"])

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
            result = research_run(
                self.service,
                {
                    **PAIR,
                    "fill": fill,
                    "targets": [
                        {
                            k: v
                            for k, v in r.items()
                            if k in ("universe", "asset", "pair")
                        }
                        for r in (PAIR, CAL)
                    ],
                },
                optimize=True,
            )
            self.assertEqual(result["status"], "maximum_found")
            self.assertEqual(result["group"]["member_count"], 2)
            self.assertEqual(
                result["group"]["execution_reference"], fill == "reference_same_bar"
            )
            self.assertTrue(
                all(m["selection"]["fill"] == fill for m in result["group"]["members"])
            )
            self.assertTrue(
                all(
                    m["selection"]["execution_reference"]
                    == (fill == "reference_same_bar")
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
            save_settings(
                self.root, {"validation_percent": share, "slippage_ticks": 2.5}
            )
            result = window_summaries(self.service, {"requests": requests})
            self.assertEqual(result["validation_percent"], share)
            for req, row in zip(requests, result["rows"]):
                proof = self.run_proof(req)
                self.assertEqual(row["status"], "measured")
                self.assertEqual(row["run_id"], proof["run_id"])
                self.assertEqual(row["trades"], proof["summary"]["trades"])
                self.assertEqual(
                    row["annualized_excess_pp"],
                    proof["evaluation"]["result"].get("annualized_excess_pp"),
                )
                self.assertEqual(row["evaluation"], proof["evaluation"])
                self.assertNotIn("ledger", row)
                self.assertNotIn("plateau", row)
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

        with patch("moex_spread_lab.proof_windows.StudyWindow", side_effect=changed):
            result = window_summaries(self.service, {"requests": [PAIR, CAL]})
        self.assertEqual(result["slippage_ticks"], 1)
        self.assertTrue(
            all(r["selection"]["slippage_ticks"] == 1 for r in result["rows"])
        )
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
        pair = next(
            p for p in catalog["pair_screen"]["pairs"] if p["id"] == PAIR["pair"]
        )
        proof = self.run_proof(PAIR)
        self.assertEqual(
            pair["passes_capital_screen"], proof["window"]["passes_capital_screen"]
        )
        self.assertEqual(
            pair["tradable_every_day"], proof["window"]["tradable_every_day"]
        )
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
