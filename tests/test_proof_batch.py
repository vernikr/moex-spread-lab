"""Fixed-cohort proof search: real disjoint fixture histories, accounting, admission and HTTP."""

import http.client
import json
import tempfile
import threading
import unittest
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from proof_fixtures import fixture

from moex_spread_lab import calendar_spreads
from moex_spread_lab.evaluation_periods import EvaluationSplit, save_settings
from moex_spread_lab.proof import ProofService
from moex_spread_lab.proof_batch import MAX_TARGETS, _targets
from moex_spread_lab.proof_search import TRIALS, _annualized_returns, research_run
from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler
from moex_spread_lab.w0 import W0Error

P = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6"}
C = {"universe": "calendar_native", "asset": "SR", "pair": "SRH3/SRM3"}
ACTIVE = {
    **P,
    "strategy": "mean_reversion",
    "parameters": {"lookback": 10, "entry_z": 1.5, "exit_z": 0.5},
}


class AnnualizationTests(unittest.TestCase):
    def test_independent_cagr_minus_benchmark_and_continuous_validation_opening(self):
        # Hand calculation: one year between marks. 3k margin held during the
        # +10% interval then released; reserve earns no LQDT. Book earns 600 net.
        days = ["2021-01-01", "2022-01-01", "2023-01-01"]
        marks = dict(zip(days, [100, 110, 121]))
        proof = {
            "ledger": [
                {"date": d, "cumulative_net_rub": v}
                for d, v in zip(days, [0, 600, 600])
            ],
            "trades": [
                {
                    "entry": {"date": days[0]},
                    "exit": {"date": days[1]},
                    "margin_rub": 3000,
                }
            ],
        }
        training = _annualized_returns(
            proof, marks, EvaluationSplit.from_dates(days[:2], 0)
        )
        self.assertEqual(training["calendar_days"], 365)
        self.assertAlmostEqual(training["strategy_annualized_pct"], 11.0)
        self.assertAlmostEqual(training["lqdt_annualized_pct"], 10.0)
        self.assertAlmostEqual(training["annualized_excess_pp"], 1.0)
        final = _annualized_returns(proof, marks, EvaluationSplit.from_dates(days, 25))
        self.assertEqual(final["calendar_days"], 365)
        self.assertEqual(final["annualized_excess_pp"], 0.0)
        whole = _annualized_returns(proof, marks, EvaluationSplit.from_dates(days, 0))
        self.assertAlmostEqual(
            whole["annualized_excess_pp"], 100 * ((33300 * 1.1 / 30000) ** 0.5 - 1.1)
        )
        for bad_days, bad_marks in [
            (days[:1], marks),
            (days, dict.fromkeys(days, 0.0)),
        ]:
            with self.subTest(days=bad_days), self.assertRaises(W0Error):
                _annualized_returns(
                    proof, bad_marks, EvaluationSplit.from_dates(bad_days, 0)
                )


class CohortTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        fixture(self.root)
        self.service = ProofService(self.root)

    def search(self, base=ACTIVE, **kw):
        return research_run(self.service, {**base, **kw}, optimize=True)

    def test_one_maximum_across_disjoint_training_windows_and_both_native_spellings(
        self,
    ):
        save_settings(self.root, {"validation_percent": 25})
        with patch.object(
            calendar_spreads, "fetch_inputs", wraps=calendar_spreads.fetch_inputs
        ) as loads:
            result = self.search(targets=[P, C])
            self.assertEqual(loads.call_count, 1)
        self.assertEqual(result, self.search(targets=[C, P]))
        self.assertEqual(result["trial_count"], TRIALS)
        self.assertEqual(result["status"], "maximum_found")
        g = result["group"]
        self.assertEqual(g["member_count"], 2)
        cal, pair = g["members"]
        self.assertEqual([cal["target"], pair["target"]], [C, P])
        self.assertLess(
            cal["evaluation"]["periods"]["validation"][-1],
            pair["evaluation"]["periods"]["training"][0],
        )
        self.assertNotEqual(
            cal["evaluation"]["observations"], pair["evaluation"]["observations"]
        )
        scores = {}
        for t in result["candidates"]:
            self.assertEqual(len(t["by_window_pp"]), 2)
            self.assertAlmostEqual(t["rank_score"], sum(t["by_window_pp"]) / 2)
            self.assertTrue(
                5 <= t["parameters"]["lookback"] <= 40
                and 0.1 <= t["parameters"]["exit_z"] < t["parameters"]["entry_z"]
            )
            scores[t["rank_score"]] = t["parameters"]
        self.assertEqual(g["training_mean_pp"], max(scores))
        params = result["selected_parameters"]
        self.assertEqual(scores[max(scores)], params)
        self.assertEqual(pair["selection"]["parameters"], params)
        for row, key, strategy in [
            (cal, "window", "zscore_mean_reversion"),
            (pair, "lookback", "mean_reversion"),
        ]:
            expected = {
                key: params["lookback"],
                "entry_z": params["entry_z"],
                "exit_z": params["exit_z"],
            }
            self.assertEqual(row["selection"]["parameters"], expected)
            self.assertEqual(row["selection"]["strategy"], strategy)
            req = row["selection"]
            rerun = research_run(self.service, req, optimize=False)
            self.assertEqual(rerun["run_id"], row["run_id"])
            self.assertEqual(rerun["evaluation"], row["evaluation"])
            # Independent ordinary replay cut on this member's own training end, not a common-date table.
            cutoff = row["evaluation"]["periods"]["training"][-1]
            prefix = self.service.prepare(req)(req, through=cutoff)
            dates = row["evaluation"]["periods"]["training"]
            prices = {
                r["date"]: r["price"]
                for r in json.loads(
                    (self.root / "data/benchmark/lqdt-lineage.json").read_text()
                )["rows"]
            }
            wealth = 30_000.0
            last_px = prices[dates[0]]
            previous_net = 0.0
            reserved = 0
            for r in prefix["ledger"]:
                d = r["date"]
                price = prices[d]
                net = r["cumulative_net_rub"]
                wealth += (
                    (wealth - reserved) * (price / last_px - 1) + net - previous_net
                )
                reserved = sum(
                    t["margin_rub"]
                    for t in prefix["trades"]
                    if t["entry"]["date"] <= d < t["exit"]["date"]
                )
                last_px = price
                previous_net = net
            years = (
                date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])
            ).days / 365
            manual = 100 * (
                (wealth / 30_000) ** (1 / years)
                - (last_px / prices[dates[0]]) ** (1 / years)
            )
            self.assertAlmostEqual(row["training_annualized_excess_pp"], manual)
        rates = [
            m["evaluation"]["result"]["annualized_excess_pp"] for m in g["members"]
        ]
        self.assertAlmostEqual(g["result_mean_pp"], sum(rates) / 2)
        self.assertNotEqual(g["training_mean_pp"], g["result_mean_pp"])
        cal_active = {
            **C,
            "strategy": "zscore_mean_reversion",
            "parameters": {"window": 10, "entry_z": 1.5, "exit_z": 0.5},
        }
        reverse = self.search(cal_active, targets=[P, C])
        self.assertEqual(reverse["selected_parameters"], cal["selection"]["parameters"])
        self.assertEqual(reverse["group"], g)

    def test_only_holdout_liquidity_changes_neither_trials_nor_winner_or_lamps(self):
        save_settings(
            self.root, {"validation_percent": 25, "allow_failed_volume": False}
        )
        before = self.search(targets=[P, C])
        path = self.root / "data/w1/candles/CRZ6_1d.json"
        data = json.loads(path.read_text())
        data["rows"][-2]["volume"] = 1
        path.write_text(json.dumps(data))
        after = self.search(targets=[P, C])
        g = after["group"]
        for k in ("status", "candidates", "selected_parameters", "plateau"):
            self.assertEqual(before[k], after[k])
        self.assertEqual(g["member_count"], 2)
        self.assertIsNone(g["result_mean_pp"])
        self.assertIsNotNone(g["training_mean_pp"])
        self.assertIn("ликвидности", g["members"][1]["evaluation"]["result"]["reason"])
        save_settings(self.root, {"validation_percent": 0})
        blocked = self.search(targets=[P, C])
        self.assertEqual(blocked["remaining_count"], 1)
        self.assertEqual(blocked["excluded"][0]["target"], P)

    def test_no_exposure_is_weighted_zero_not_a_dropped_member(self):
        path = self.root / "data/w1/candles/CRZ6_1d.json"
        far = json.loads(path.read_text())
        near = json.loads((path.parent / "CNYRUBF_1d.json").read_text())
        for r, n in zip(far["rows"], near["rows"]):
            for k in ("open", "close", "high", "low"):
                r[k] = n[k]
        path.write_text(json.dumps(far))
        g = self.search(targets=[C, P])
        self.assertEqual(g["status"], "maximum_found")
        for t in g["candidates"]:
            self.assertEqual(t["by_window_pp"][1], 0)
            self.assertEqual(t["rank_score"], t["by_window_pp"][0] / 2)
        m = g["group"]["members"][1]
        self.assertIn("спреда не было", m["evaluation"]["result"]["reason"])
        self.assertEqual(m["training_annualized_excess_pp"], 0)
        self.assertGreater(g["group"]["training_mean_pp"], 0)

    def test_failed_member_prunes_the_whole_proposal_not_the_denominator(self):
        from moex_spread_lab import proof_search

        original = proof_search._measure

        def fail_some(proof, marks, split, settings):
            if (
                proof["selection"]["strategy"] == "mean_reversion"
                and proof["selection"]["parameters"]["lookback"] > 10
            ):
                return {"utility": None, "reason": "unmeasurable member"}
            return original(proof, marks, split, settings)

        with patch.object(proof_search, "_measure", side_effect=fail_some):
            r = self.search(targets=[C, P])
        self.assertTrue(any(t["rank_score"] is None for t in r["candidates"]))
        self.assertLessEqual(r["selected_parameters"]["lookback"], 10)
        for t in r["candidates"]:
            if t["parameters"]["lookback"] > 10:
                self.assertIsNone(t["rank_score"])
                self.assertIn(P["pair"], t["reason"])
        with patch.object(
            proof_search,
            "_measure",
            return_value={"utility": None, "reason": "unmeasurable"},
        ):
            none = self.search(targets=[C, P])
            self.assertEqual(none["status"], "no_admissible_candidate")
            self.assertIsNone(none["selected_parameters"])
            self.assertIsNone(none["group"]["training_mean_pp"])

    def test_explicit_rejections_return_before_optuna_and_empty_is_focus_only(self):
        single = self.search(targets=[])
        self.assertEqual(single, self.search())
        self.assertEqual(single, self.search(targets=[P]))
        with patch(
            "moex_spread_lab.proof_batch._tune",
            side_effect=AssertionError("no pre-confirmation search"),
        ):
            for req, targets, excluded in [
                ({**P, "strategy": "funding_only"}, [P, C], [C]),
                ({**P, "strategy": "funding_only"}, [C], [C]),
                ({**C, "strategy": "convergence_to_ruonia"}, [P, C], [P]),
                (
                    {**C, "strategy": "zscore_mean_reversion", "fill": "next_close"},
                    [C, P],
                    [P],
                ),
                (
                    {**C, "strategy": "convergence_to_ruonia"},
                    [{**C, "asset": "RI"}],
                    [{**C, "asset": "RI"}],
                ),
            ]:
                with self.subTest(req=req, targets=targets):
                    r = self.search(req, targets=targets)
                    self.assertEqual(r["status"], "selection_requires_confirmation")
                    self.assertEqual([x["target"] for x in r["excluded"]], excluded)
                    self.assertEqual(r["remaining_count"], len(targets) - len(excluded))
                    self.assertTrue(all(x["reason"] for x in r["excluded"]))
                    self.assertEqual(r["trial_count"], 0)
        for raw in (
            [P, P],
            [{**P, "parameters": {}}],
            [{**C, "asset": None}],
            None,
            [P] * (MAX_TARGETS + 1),
        ):
            with self.assertRaises(W0Error):
                _targets({**ACTIVE, "targets": raw})

    def test_large_confirmation_preview_is_not_logged_and_searches_are_serialized(self):
        store = ScreenerStore(ScreenerInputs(self.root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
        self.addCleanup(conn.close)
        req = {
            **P,
            "strategy": "funding_only",
            "targets": [P, *[{**C, "pair": f"W{i}/T{i}"} for i in range(200)]],
        }
        data = json.dumps(req)
        self.assertGreater(len(data), 8192)

        def post():
            conn.request(
                "POST",
                "/api/proof/optimize",
                data,
                {"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            return resp.status, json.load(resp)

        with store.proof_search_lock:
            self.assertEqual(post()[0], 409)
        status, result = post()
        self.assertEqual(
            (status, len(result["excluded"]), result["remaining_count"]), (200, 200, 1)
        )
        self.assertFalse((self.root / "data/analytics/proof-searches.jsonl").exists())
        with self.assertRaises(ConnectionAbortedError):
            research_run(
                self.service,
                ACTIVE,
                optimize=True,
                check_cancelled=lambda: (_ for _ in ()).throw(ConnectionAbortedError()),
            )
