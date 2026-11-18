import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from test_calendar_proof import _build_root as calendar_fixture
from test_execution_proof import _build_root as pair_fixture

from moex_spread_lab.evaluation_periods import (
    EvaluationSplit,
    read_settings,
    save_settings,
)
from moex_spread_lab.proof import ProofService
from moex_spread_lab.proof_search import (
    EXECUTION_AXES,
    GREEN,
    STRATEGY_AXES,
    TRIALS,
    Axis,
    Landscape,
    _excess_curve,
    _measure,
    _tune,
    research_run,
)
from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler
from moex_spread_lab.w0 import W0Error

PAIR = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"}
CALENDAR = {"universe": "calendar_native", "asset": "SR", "pair": "SRH3/SRM3"}


class PlateauTests(unittest.TestCase):
    def test_one_axis_probes_with_constraints_missing_and_negative_results(self):
        axes = STRATEGY_AXES["mean_reversion"] | EXECUTION_AXES
        seed = {
            "lookback": 10,
            "entry_z": 1.0,
            "exit_z": 0.9,
            "quantity": 1,
            "slippage_ticks": 1.0,
        }
        seen = []

        def evaluate(p):
            seen.append(p)
            self.assertLess(p["exit_z"], p["entry_z"])
            if p["quantity"] > 1:
                raise W0Error("capital")
            return {"utility": 100 - 20 * abs(p["lookback"] - 10)}

        land = Landscape(axes, evaluate)
        window = land.profile(seed, "lookback")
        self.assertEqual(window["score"], 60)
        self.assertEqual([n["value"] for n in window["neighbors"]], [8, 9, 11, 12])
        self.assertTrue(all(p["entry_z"] == 1.0 for p in seen))
        entry = land.profile(seed, "entry_z")
        self.assertEqual(entry["score"], 100)
        self.assertEqual([n["value"] for n in entry["neighbors"]], [1.1, 1.2])
        blocked = land.profile(seed, "quantity")
        self.assertIsNone(blocked["score"])
        self.assertTrue(all(n["reason"] == "capital" for n in blocked["neighbors"]))
        negative = Landscape({"window": Axis(3, 60, 1)}, lambda _: {"utility": -2})
        self.assertEqual(negative.profile({"window": 20}, "window")["score"], 0)

    def test_tpe_prefers_a_positive_neighborhood_to_a_sharp_profit_peak(self):
        axes = STRATEGY_AXES["funding_only"]
        seed = {"lookback": 28, "quantity": 1, "slippage_ticks": 1.5}

        def objective(p):
            self.assertEqual((p["quantity"], p["slippage_ticks"]), (1, 1.5))
            v = p["lookback"]
            return {"utility": 200 if v == 28 else 70 if 8 <= v <= 23 else -1}

        land = Landscape(axes | EXECUTION_AXES, objective)
        chosen, tries, _ = _tune(land, seed, axes, 100)
        self.assertEqual(tries, TRIALS)
        self.assertIn(chosen["lookback"], range(10, 22))
        self.assertGreaterEqual(land.profile(chosen, "lookback")["score"], GREEN)
        flat = Landscape(axes | EXECUTION_AXES, lambda _: {"utility": 0})
        self.assertIsNone(_tune(flat, seed, axes, 100)[0])

    def test_missing_exposure_names_each_period_and_counts_a_carried_spread(self):
        dates = [f"2026-11-{day:02}" for day in range(1, 9)]
        split = EvaluationSplit.from_dates(dates, 50)
        marks = dict.fromkeys(dates, 1.0)
        for span, missing in (
            ((5, 6), "только на обучении"),
            ((1, 4), "только на проверке"),
            (None, "ни на обучении, ни на проверке"),
            (
                (4, 5),
                None,
            ),  # Enter on the last training date, exit on the first validation date.
        ):
            with self.subTest(span=span):
                trades = (
                    []
                    if span is None
                    else [
                        {
                            "entry": {"date": dates[span[0] - 1]},
                            "exit": {"date": dates[span[1] - 1]},
                            "margin_rub": 100,
                            "lqdt_on_margin_rub": 0,
                        }
                    ]
                )
                proof = {
                    "selection": {"strategy": "funding_only"},
                    "window": {
                        "passes_capital_screen": True,
                        "tradable_every_day": True,
                    },
                    "ledger": [{"date": day, "cumulative_net_rub": 0} for day in marks],
                    "trades": trades,
                }
                measured = _measure(proof, marks, split)
                if missing is None:
                    self.assertEqual(
                        measured["utility"], 0
                    )  # Measured flat result, not missing evidence.
                    self.assertNotIn("reason", measured)
                else:
                    self.assertIsNone(measured["utility"])
                    self.assertEqual(
                        measured["reason"], f"Открытого спреда не было {missing}."
                    )

    def test_mark_change_including_first_validation_session_not_exit_trade_sum(self):
        days = list("abcd")
        proof = {
            "selection": {"strategy": "funding_only"},
            "window": {"passes_capital_screen": True, "tradable_every_day": True},
            "ledger": [
                {"date": d, "cumulative_net_rub": x}
                for d, x in zip(days, [0, 10, 70, 72])
            ],
            "trades": [
                {
                    "entry": {"date": "a"},
                    "exit": {"date": "d"},
                    "margin_rub": 100,
                    "lqdt_on_margin_rub": 4,
                }
            ],
        }
        marks = dict(zip(days, (1, 1.01, 1.02, 1.04)))
        for percent, expected in [
            (0, (72, 68, 17)),
            (50, (62, 59, 29.5)),
            (1, (2, 0, 0)),
        ]:
            with self.subTest(percent=percent):
                m = _measure(proof, marks, EvaluationSplit.from_dates(days, percent))
                self.assertEqual(
                    (m["net_rub"], m["excess_rub"], m["utility"]), expected
                )
        proof["trades"] = []
        no_check = _measure(proof, marks, EvaluationSplit.from_dates(days, 0))
        self.assertEqual(no_check["reason"], "Открытого спреда не было на обучении.")
        self.assertIsNone(no_check["validation"])

    def test_daily_hurdle_marks_positions_across_a_split_and_reconciles_at_exit(self):
        proof = {
            "ledger": [
                {"date": d, "cumulative_net_rub": x}
                for d, x in zip("abcd", [0, 10, 20, 31])
            ],
            "trades": [
                {
                    "entry": {"date": "a"},
                    "exit": {"date": "c"},
                    "margin_rub": 100,
                    "lqdt_on_margin_rub": 3.33,
                }
            ],
        }
        curve = _excess_curve(proof, {"a": 1, "b": 1.01, "c": 1.0333333, "d": 1.5})
        self.assertAlmostEqual(curve["b"], 9)
        self.assertAlmostEqual(curve["d"], 31 - 3.33)


class ProofSearchIntegrationTests(unittest.TestCase):
    def fixture(self, builder):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        builder(root)
        return ProofService(root)

    def test_pair_replay_reproducibility_and_holdout_is_not_a_selection_input(self):
        proof = self.fixture(pair_fixture)
        save_settings(proof.root, {"validation_percent": 25})
        req = {**PAIR, "slippage_ticks": 0.5}
        a = research_run(proof, req, optimize=True)
        self.assertEqual(a, research_run(proof, req, optimize=True))
        self.assertEqual(a["method"], "Optuna TPESampler")
        self.assertEqual(a["trial_count"], TRIALS)
        self.assertEqual([c["trial"] for c in a["candidates"]], list(range(TRIALS)))
        self.assertEqual(a["status"], "plateau_found")
        self.assertEqual(
            set(a["selected_parameters"]), {"lookback", "entry_z", "exit_z"}
        )
        picked = {**req, "parameters": a["selected_parameters"]}
        displayed = research_run(proof, picked, optimize=False)
        self.assertEqual(displayed["plateau"], a["plateau"])
        self.assertEqual(displayed["evaluation"], a["evaluation"])
        self.assertEqual(displayed["run_id"], a["result_run_id"])
        self.assertEqual(displayed["proof_run_id"], proof.proof(picked)["run_id"])
        self.assertEqual(set(a["evaluation"]["periods"]), {"training", "validation"})
        start = a["evaluation"]["periods"]["validation"][0]
        path = proof.root / "data/w1/candles/CRZ6_1d.json"
        data = json.loads(path.read_text())
        for row in data["rows"]:
            if row["begin"][:10] >= start:
                for key in ("open", "high", "low", "close"):
                    row[key] += 4
        path.write_text(json.dumps(data))
        changed = research_run(proof, req, optimize=True)
        self.assertNotEqual(a["input_run_id"], changed["input_run_id"])
        self.assertNotEqual(
            a["evaluation"]["result"]["excess_rub"],
            changed["evaluation"]["result"]["excess_rub"],
        )
        for key in ("selected_parameters", "candidates", "plateau"):
            self.assertEqual(a[key], changed[key])

    def test_0_and_50_share_change_result_not_history_or_signals(self):
        for fixture, req in (
            (
                pair_fixture,
                {**PAIR, "parameters": {"lookback": 10, "entry_z": 1, "exit_z": 0.5}},
            ),
            (calendar_fixture, {**CALENDAR, "strategy": "zscore_mean_reversion"}),
        ):
            service = self.fixture(fixture)
            whole = research_run(service, req, optimize=False, include_plateau=False)
            self.assertNotIn("plateau", whole)
            e = whole["evaluation"]
            self.assertEqual(e["result_basis"], "training")
            self.assertIsNone(e["periods"]["validation"])
            self.assertEqual(e["observations"]["validation"], 0)
            self.assertAlmostEqual(
                e["result"]["net_rub"], whole["summary"]["net_rub"], delta=0.03
            )
            save_settings(service.root, {"validation_percent": 50})
            half = research_run(service, req, optimize=False, include_plateau=False)
            for k in ("proof_run_id", "series", "trades", "ledger", "summary"):
                self.assertEqual(whole[k], half[k])
            self.assertNotEqual(whole["run_id"], half["run_id"])
            h = half["evaluation"]
            self.assertEqual(h["result_basis"], "validation")
            self.assertEqual(
                h["observations"]["validation"], e["observations"]["training"] // 2
            )
            marks = {r["date"]: r["cumulative_net_rub"] for r in half["ledger"]}
            self.assertAlmostEqual(
                h["result"]["net_rub"],
                marks[h["periods"]["validation"][-1]]
                - marks[h["periods"]["training"][-1]],
            )

    def test_journal_only_rows_do_not_move_the_validation_boundary(self):
        service = self.fixture(pair_fixture)
        save_settings(service.root, {"validation_percent": 50})
        path = service.root / "data/w1/candles/CRZ6_1d.json"
        data = json.loads(path.read_text())
        for i in (0, 27, -1):
            data["rows"][i]["volume"] = 0
        path.write_text(json.dumps(data))
        result = research_run(service, PAIR, optimize=False)
        e = result["evaluation"]
        self.assertEqual(len(result["ledger"]), 55)
        self.assertEqual(e["observations"], {"training": 26, "validation": 26})
        self.assertEqual(
            e["periods"]["validation"],
            [data["rows"][28]["begin"][:10], data["rows"][-2]["begin"][:10]],
        )

    def test_validation_liquidity_failure_does_not_veto_calendar_training_plateau(self):
        service = self.fixture(calendar_fixture)
        save_settings(service.root, {"validation_percent": 25})
        req = {**CALENDAR, "strategy": "zscore_mean_reversion"}
        before = research_run(service, req, optimize=True)
        self.assertIsNotNone(before["selected_parameters"])
        path = next(
            (service.root / "data/spread-history-cache").glob("SRH3SRM3-*.json")
        )
        data = json.loads(path.read_text())
        history = data["payload"]["history"]
        dates, volume = (history["columns"].index(k) for k in ("TRADEDATE", "VOLUME"))
        start = before["evaluation"]["periods"]["validation"][0]
        next(row for row in history["data"] if row[dates] == start)[volume] = 1
        path.write_text(json.dumps(data))
        after = research_run(service, req, optimize=True)
        for k in ("status", "selected_parameters", "candidates", "plateau"):
            self.assertEqual(before[k], after[k])
        self.assertIn("ликвидности", after["evaluation"]["result"]["reason"])

    def test_funding_and_both_calendar_strategies_use_only_active_fields(self):
        pair, cal = self.fixture(pair_fixture), self.fixture(calendar_fixture)
        for service, req, active in (
            (pair, {**PAIR, "strategy": "funding_only"}, {"lookback"}),
            (
                cal,
                {**CALENDAR, "strategy": "zscore_mean_reversion"},
                {"window", "entry_z", "exit_z"},
            ),
            (cal, {**CALENDAR, "strategy": "convergence_to_ruonia"}, {"threshold_pp"}),
        ):
            with self.subTest(strategy=req["strategy"]):
                result = research_run(service, req, optimize=True)
                json.dumps(result, allow_nan=False)
                self.assertEqual(result["trial_count"], TRIALS)
                self.assertEqual(
                    set(result["plateau"]["fields"]), active | EXECUTION_AXES.keys()
                )
                self.assertTrue(
                    all(set(t["parameters"]) == active for t in result["candidates"])
                )
        no_capital = research_run(cal, {**CALENDAR, "quantity": 3}, optimize=True)
        self.assertEqual(no_capital["status"], "unavailable")
        self.assertEqual(no_capital["trial_count"], 0)
        self.assertIsNone(no_capital["selected_parameters"])
        self.assertTrue(
            all(f["score"] is None for f in no_capital["plateau"]["fields"].values())
        )

    def test_short_series_and_invalid_values_never_produce_a_fake_plateau(self):
        proof = self.fixture(pair_fixture)
        path = proof.root / "data/w1/candles/CRZ6_1d.json"
        data = json.loads(path.read_text())
        data["rows"] = data["rows"][:18]
        path.write_text(json.dumps(data))
        result = research_run(proof, PAIR, optimize=True)
        self.assertIn("18", result["reason"])
        self.assertEqual(result["status"], "unavailable")
        for params in (
            {"lookback": 6.5},
            {"lookback": True},
            {"entry_z": 1, "exit_z": 2},
            {"exit_z": float("nan")},
            {"threshold_pp": 1},
        ):
            with self.assertRaises(W0Error):
                research_run(proof, {**PAIR, "parameters": params}, optimize=True)

    def test_http_home_alias_new_search_rerun_and_local_origin_boundary(self):
        proof = self.fixture(pair_fixture)
        inputs = ScreenerInputs(proof.root)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(ScreenerStore(inputs))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        self.addCleanup(conn.close)
        pages = []
        for path in ("/?universe=pair_screen", "/proof?universe=calendar_native"):
            conn.request("GET", path)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            pages.append(response.read())
        self.assertEqual(pages[0], pages[1])
        self.assertIn(b'id="auto-tune"', pages[0])
        self.assertNotIn("К скринеру пар".encode(), pages[0])
        headers = {"Content-Type": "application/json"}
        conn.request("GET", "/api/settings")
        self.assertEqual(json.load(conn.getresponse()), {"validation_percent": 0})
        for value, status, origin in [
            (51, 400, None),
            (-1, 400, None),
            (True, 400, None),
            ("25", 400, None),
            (25, 403, "https://example.org"),
            (25, 200, None),
        ]:
            with self.subTest(value=value, origin=origin):
                conn.request(
                    "POST",
                    "/api/settings",
                    json.dumps({"validation_percent": value}),
                    {**headers, **({"Origin": origin} if origin else {})},
                )
                r = conn.getresponse()
                r.read()
                self.assertEqual(r.status, status)
                self.assertEqual(
                    read_settings(proof.root)["validation_percent"],
                    25 if status == 200 else 0,
                )
        conn.request("POST", "/api/proof/run", json.dumps(PAIR), headers)
        plain = json.load(conn.getresponse())
        self.assertEqual(plain["evaluation"]["validation_percent"], 25)
        self.assertNotIn("plateau", plain)
        conn.request(
            "POST",
            "/api/proof/optimize",
            json.dumps(PAIR),
            {**headers, "Origin": "https://example.org"},
        )
        denied = conn.getresponse()
        self.assertEqual(denied.status, 403)
        denied.read()
        conn.request("POST", "/api/proof/optimize", json.dumps(PAIR), headers)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        result = json.loads(response.read())
        self.assertIsNotNone(result["selected_parameters"])
        conn.request(
            "POST",
            "/api/proof/run",
            json.dumps(
                {
                    **PAIR,
                    "parameters": result["selected_parameters"],
                    "include_plateau": True,
                }
            ),
            headers,
        )
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        displayed = json.loads(response.read())
        self.assertEqual(
            displayed["selection"]["parameters"], result["selected_parameters"]
        )
        self.assertEqual(displayed["plateau"], result["plateau"])
        self.assertEqual(
            result["run_id"],
            json.loads((inputs.history.parent / "proof-searches.jsonl").read_text())[
                "run_id"
            ],
        )


if __name__ == "__main__":
    unittest.main()
