"""Proof-search regressions: landscape, research runs and loopback boundary.

P7.2 leaf #131 keeps every test case ID in this module while the assertion-rich
bodies live in uniquely named helper modules below the code-health size ceiling.
No test, assertion, subtest or fixture behaviour is changed.
"""

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from moex_spread_lab.core.settings import (
    EvaluationSplit,
    ResearchSettings,
    read_settings,
    save_settings,
)
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.proof_search import (
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
from moex_spread_lab.proof.service import ProofService
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs
from moex_spread_lab.web.server import ScreenerStore, make_handler
from test_calendar_proof import _build_root as calendar_fixture
from test_execution_proof import _build_root as pair_fixture


# ---- folded from the retired P7.2 shard proof_search_service_p72_packages_proof_tests_test_proof_search ----
def check_global_seed_makes_a_run_reproducible_and_zero_draws_a_new_one(case):
    proof = case.fixture(pair_fixture)
    req = {**PAIR, "slippage_ticks": 0.5}
    # A fixed seed reproduces the whole search, proposal by proposal.
    save_settings(proof.root, {"random_seed": 41})
    first = research_run(proof, req, optimize=True)
    case.assertEqual(first["seed"], 41)
    case.assertEqual(first["seed_used"], 41)
    again = research_run(proof, req, optimize=True)
    case.assertEqual(first["candidates"], again["candidates"])
    case.assertEqual(first["selected_parameters"], again["selected_parameters"])
    # Seed 0 draws a fresh seed per run, so the search is not pinned.
    save_settings(proof.root, {"random_seed": 0})
    drawn = [research_run(proof, req, optimize=True) for _ in range(3)]
    for run in drawn:
        case.assertEqual(run["seed"], 0)
        case.assertGreater(run["seed_used"], 0)
    case.assertGreater(len({run["seed_used"] for run in drawn}), 1)


def check_pair_replay_reproducibility_and_holdout_is_not_a_selection_input(case):
    proof = case.fixture(pair_fixture)
    # A fixed seed keeps two runs comparable; seed 0 would draw a new one.
    save_settings(proof.root, {"validation_percent": 25, "random_seed": 41})
    req = {**PAIR, "slippage_ticks": 0.5}
    a = research_run(proof, req, optimize=True)
    case.assertEqual(a, research_run(proof, req, optimize=True))
    case.assertEqual(a["method"], "Optuna TPESampler")
    case.assertEqual(a["trial_count"], TRIALS)
    case.assertEqual([c["trial"] for c in a["candidates"]], list(range(TRIALS)))
    case.assertEqual(a["status"], "maximum_found")
    case.assertEqual(set(a["selected_parameters"]), {"lookback", "entry_z", "exit_z"})
    picked = {**req, "parameters": a["selected_parameters"]}
    displayed = research_run(proof, picked, optimize=False)
    case.assertEqual(displayed["plateau"], a["plateau"])
    case.assertEqual(displayed["evaluation"], a["evaluation"])
    case.assertEqual(displayed["run_id"], a["result_run_id"])
    case.assertEqual(displayed["proof_run_id"], proof.proof(picked)["run_id"])
    case.assertEqual(set(a["evaluation"]["periods"]), {"training", "validation"})
    start = a["evaluation"]["periods"]["validation"][0]
    path = proof.root / "data/w1/candles/CRZ6_1d.json"
    data = json.loads(path.read_text())
    for row in data["rows"]:
        if row["begin"][:10] >= start:
            for key in ("open", "high", "low", "close"):
                row[key] += 4
    path.write_text(json.dumps(data))
    changed = research_run(proof, req, optimize=True)
    case.assertNotEqual(a["input_run_id"], changed["input_run_id"])
    case.assertNotEqual(
        a["evaluation"]["result"]["excess_rub"],
        changed["evaluation"]["result"]["excess_rub"],
    )
    for key in ("selected_parameters", "candidates", "plateau"):
        case.assertEqual(a[key], changed[key])


def check_0_and_50_share_change_result_not_history_or_signals(case):
    for fixture, req in (
        (
            pair_fixture,
            {**PAIR, "parameters": {"lookback": 10, "entry_z": 1, "exit_z": 0.5}},
        ),
        (calendar_fixture, {**CALENDAR, "strategy": "zscore_mean_reversion"}),
    ):
        service = case.fixture(fixture)
        whole = research_run(service, req, optimize=False, include_plateau=False)
        case.assertNotIn("plateau", whole)
        e = whole["evaluation"]
        case.assertEqual(e["result_basis"], "training")
        case.assertIsNone(e["periods"]["validation"])
        case.assertEqual(e["observations"]["validation"], 0)
        case.assertAlmostEqual(e["result"]["net_rub"], whole["summary"]["net_rub"], delta=0.03)
        save_settings(service.root, {"validation_percent": 50})
        half = research_run(service, req, optimize=False, include_plateau=False)
        for k in ("proof_run_id", "series", "trades", "ledger", "summary"):
            case.assertEqual(whole[k], half[k])
        case.assertNotEqual(whole["run_id"], half["run_id"])
        h = half["evaluation"]
        case.assertEqual(h["result_basis"], "validation")
        case.assertEqual(h["observations"]["validation"], e["observations"]["training"] // 2)
        marks = {r["date"]: r["cumulative_net_rub"] for r in half["ledger"]}
        case.assertAlmostEqual(
            h["result"]["net_rub"],
            marks[h["periods"]["validation"][-1]] - marks[h["periods"]["training"][-1]],
        )


def check_journal_only_rows_do_not_move_the_validation_boundary(case):
    service = case.fixture(pair_fixture)
    save_settings(service.root, {"validation_percent": 50})
    path = service.root / "data/w1/candles/CRZ6_1d.json"
    data = json.loads(path.read_text())
    for i in (0, 27, -1):
        data["rows"][i]["volume"] = 0
    path.write_text(json.dumps(data))
    result = research_run(service, PAIR, optimize=False)
    e = result["evaluation"]
    case.assertEqual(len(result["ledger"]), 55)
    case.assertEqual(e["observations"], {"training": 26, "validation": 26})
    case.assertEqual(
        e["periods"]["validation"],
        [data["rows"][28]["begin"][:10], data["rows"][-2]["begin"][:10]],
    )


def check_validation_liquidity_failure_does_not_veto_calendar_training_plateau(case):
    service = case.fixture(calendar_fixture)
    save_settings(
        service.root,
        # A fixed seed keeps the two runs comparable; seed 0 would draw a new one.
        {"validation_percent": 25, "allow_failed_volume": False, "random_seed": 41},
    )
    req = {**CALENDAR, "strategy": "zscore_mean_reversion"}
    before = research_run(service, req, optimize=True)
    case.assertIsNotNone(before["selected_parameters"])
    path = next((service.root / "data/spread-history-cache").glob("SRH3SRM3-*.json"))
    data = json.loads(path.read_text())
    history = data["payload"]["history"]
    dates, volume = (history["columns"].index(k) for k in ("TRADEDATE", "VOLUME"))
    start = before["evaluation"]["periods"]["validation"][0]
    next(row for row in history["data"] if row[dates] == start)[volume] = 1
    path.write_text(json.dumps(data))
    after = research_run(service, req, optimize=True)
    for k in ("status", "selected_parameters", "candidates", "plateau"):
        case.assertEqual(before[k], after[k])
    case.assertIn("ликвидности", after["evaluation"]["result"]["reason"])


def check_funding_and_both_calendar_strategies_use_only_active_fields(case):
    pair, cal = case.fixture(pair_fixture), case.fixture(calendar_fixture)
    for service, req, active in (
        (pair, {**PAIR, "strategy": "funding_only"}, {"lookback"}),
        (
            cal,
            {**CALENDAR, "strategy": "zscore_mean_reversion"},
            {"window", "entry_z", "exit_z"},
        ),
        (cal, {**CALENDAR, "strategy": "convergence_to_ruonia"}, {"threshold_pp"}),
    ):
        with case.subTest(strategy=req["strategy"]):
            result = research_run(service, req, optimize=True)
            json.dumps(result, allow_nan=False)
            case.assertEqual(result["trial_count"], TRIALS)
            case.assertEqual(set(result["plateau"]["fields"]), active | EXECUTION_AXES.keys())
            case.assertTrue(all(set(t["parameters"]) == active for t in result["candidates"]))
    save_settings(cal.root, {"allow_failed_capital": False})
    no_capital = research_run(cal, {**CALENDAR, "quantity": 3}, optimize=True)
    case.assertEqual(no_capital["status"], "unavailable")
    case.assertEqual(no_capital["trial_count"], 0)
    case.assertIsNone(no_capital["selected_parameters"])
    case.assertTrue(all(f["score"] is None for f in no_capital["plateau"]["fields"].values()))


def check_short_series_and_invalid_values_never_produce_a_fake_plateau(case):
    proof = case.fixture(pair_fixture)
    path = proof.root / "data/w1/candles/CRZ6_1d.json"
    data = json.loads(path.read_text())
    data["rows"] = data["rows"][:18]
    path.write_text(json.dumps(data))
    result = research_run(proof, PAIR, optimize=True)
    case.assertIsNotNone(result["selected_parameters"])
    for lamp in result["plateau"]["fields"].values():
        case.assertIsNone(lamp["score"])
        case.assertIn("18", lamp["reason"])
    data["rows"] = data["rows"][:5]
    path.write_text(json.dumps(data))
    too_short = research_run(proof, PAIR, optimize=True)
    case.assertEqual(too_short["status"], "unavailable")
    case.assertIsNone(too_short["selected_parameters"])
    for params in (
        {"lookback": 6.5},
        {"lookback": True},
        {"entry_z": 1, "exit_z": 2},
        {"exit_z": float("nan")},
        {"threshold_pp": 1},
    ):
        with case.assertRaises(W0Error):
            research_run(proof, {**PAIR, "parameters": params}, optimize=True)


# ---- folded from the retired P7.2 shard proof_search_plateau_p72_packages_proof_tests_test_proof_search ----
def check_one_axis_probes_with_constraints_missing_and_negative_results(case):
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
        case.assertLess(p["exit_z"], p["entry_z"])
        if p["quantity"] > 1:
            raise W0Error("capital")
        return {"utility": 100 - 20 * abs(p["lookback"] - 10)}

    land = Landscape(axes, evaluate)
    window = land.profile(seed, "lookback")
    case.assertEqual(window["score"], 60)
    case.assertEqual([n["value"] for n in window["neighbors"]], [8, 9, 11, 12])
    case.assertTrue(all(p["entry_z"] == 1.0 for p in seen))
    entry = land.profile(seed, "entry_z")
    case.assertEqual(entry["score"], 100)
    case.assertEqual([n["value"] for n in entry["neighbors"]], [1.1, 1.2])
    blocked = land.profile(seed, "quantity")
    case.assertIsNone(blocked["score"])
    case.assertTrue(all(n["reason"] == "capital" for n in blocked["neighbors"]))
    negative = Landscape({"window": Axis(3, 60, 1)}, lambda _: {"utility": -2})
    case.assertEqual(negative.profile({"window": 20}, "window")["score"], 0)


def check_tpe_selects_the_highest_score_even_at_an_unstable_peak(case):
    axes = STRATEGY_AXES["funding_only"]
    seed = {"lookback": 28, "quantity": 1, "slippage_ticks": 1.5}

    def objective(p):
        case.assertEqual((p["quantity"], p["slippage_ticks"]), (1, 1.5))
        v = p["lookback"]
        return {"utility": 200 if v == 28 else 70 if 8 <= v <= 23 else -1}

    land = Landscape(axes | EXECUTION_AXES, objective)
    chosen, tries, _ = _tune(land, seed, axes, 100)
    case.assertEqual(tries, TRIALS)
    case.assertEqual(chosen["lookback"], 28)
    case.assertLess(land.profile(chosen, "lookback")["score"], GREEN)
    flat = Landscape(axes | EXECUTION_AXES, lambda _: {"utility": 0})
    case.assertEqual(_tune(flat, seed, axes, 100)[0], seed)
    losing = Landscape(axes | EXECUTION_AXES, lambda p: {"utility": -1 - abs(28 - p["lookback"])})
    case.assertEqual(_tune(losing, seed, axes, 100)[0], seed)


def check_missing_exposure_names_each_period_and_counts_a_carried_spread(case):
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
        with case.subTest(span=span):
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
            measured = _measure(proof, marks, split, ResearchSettings())
            if missing is None:
                case.assertEqual(
                    measured["utility"], 0
                )  # Measured flat result, not missing evidence.
                case.assertNotIn("reason", measured)
            else:
                case.assertIsNone(measured["utility"])
                case.assertEqual(measured["reason"], f"Открытого спреда не было {missing}.")


def check_benchmark_relative_drawdown_and_shortfall_no_longer_veto_a_candidate(case):
    # A book that falls far behind LQDT stays a candidate: the measured
    # excess is the score, and the risk path stays visible in the proof.
    # The account stays solvent, so the annualization guard still applies.
    days = [f"2026-01-0{i}" for i in range(1, 5)]
    proof = {
        "selection": {"strategy": "funding_only"},
        "window": {"passes_capital_screen": True, "tradable_every_day": True},
        "sizing": {"initial_capital_rub": 100_000.0},
        "ledger": [
            {"date": d, "cumulative_net_rub": x}
            for d, x in zip(days, [0, -12_000, -16_000, -18_000], strict=False)
        ],
        "trades": [
            {
                "entry": {"date": days[0]},
                "exit": {"date": days[-1]},
                "margin_rub": 100,
                "lqdt_on_margin_rub": 4,
            }
        ],
    }
    marks = dict.fromkeys(days, 1.0)
    measured = _measure(proof, marks, EvaluationSplit.from_dates(days, 0), ResearchSettings())
    case.assertIsNotNone(measured["rank_score"])
    case.assertIsNotNone(measured["utility"])
    case.assertNotIn("просадки", str(measured.get("reason", "")))
    case.assertNotIn("отставания", str(measured.get("reason", "")))


def check_mark_change_including_first_validation_session_not_exit_trade_sum(case):
    days = [f"2026-01-0{i}" for i in range(1, 5)]
    proof = {
        "selection": {"strategy": "funding_only"},
        "window": {"passes_capital_screen": True, "tradable_every_day": True},
        "ledger": [
            {"date": d, "cumulative_net_rub": x}
            for d, x in zip(days, [0, 10, 70, 72], strict=False)
        ],
        "trades": [
            {
                "entry": {"date": days[0]},
                "exit": {"date": days[-1]},
                "margin_rub": 100,
                "lqdt_on_margin_rub": 4,
            }
        ],
    }
    marks = dict(zip(days, (1, 1.01, 1.02, 1.04), strict=False))
    for percent, expected in [
        (0, (72, 68, 17)),
        (50, (62, 59, 29.5)),
        (1, (2, 0, 0)),
    ]:
        with case.subTest(percent=percent):
            m = _measure(
                proof,
                marks,
                EvaluationSplit.from_dates(days, percent),
                ResearchSettings(),
            )
            case.assertEqual((m["net_rub"], m["excess_rub"], m["rub_per_session"]), expected)
    proof["trades"] = []
    no_check = _measure(proof, marks, EvaluationSplit.from_dates(days, 0), ResearchSettings())
    case.assertEqual(no_check["reason"], "Открытого спреда не было на обучении.")
    case.assertIsNone(no_check["validation"])


def check_daily_hurdle_marks_positions_across_a_split_and_reconciles_at_exit(case):
    proof = {
        "ledger": [
            {"date": d, "cumulative_net_rub": x}
            for d, x in zip("abcd", [0, 10, 20, 31], strict=False)
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
    case.assertAlmostEqual(curve["b"], 9)
    case.assertAlmostEqual(curve["d"], 31 - 3.33)


# ---- folded from the retired P7.2 shard proof_search_http_p72_packages_proof_tests_test_proof_search ----
def check_http_home_alias_new_search_rerun_and_local_origin_boundary(case):
    proof = case.fixture(pair_fixture)
    inputs = ScreenerInputs(proof.root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ScreenerStore(inputs)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    case.addCleanup(server.server_close)
    case.addCleanup(server.shutdown)
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    case.addCleanup(conn.close)
    pages = []
    for path in ("/?universe=pair_screen", "/proof?universe=calendar_native"):
        conn.request("GET", path)
        response = conn.getresponse()
        case.assertEqual(response.status, 200)
        pages.append(response.read())
    case.assertEqual(pages[0], pages[1])
    case.assertIn(b'id="auto-tune"', pages[0])
    case.assertNotIn("К скринеру пар".encode(), pages[0])
    headers = {"Content-Type": "application/json"}
    conn.request("GET", "/api/settings")
    case.assertEqual(json.load(conn.getresponse()), ResearchSettings().metadata())
    for value, status, origin in [
        (51, 400, None),
        (-1, 400, None),
        (True, 400, None),
        ("25", 400, None),
        (25, 403, "https://example.org"),
        (25, 200, None),
    ]:
        with case.subTest(value=value, origin=origin):
            conn.request(
                "POST",
                "/api/settings",
                json.dumps({"validation_percent": value}),
                {**headers, **({"Origin": origin} if origin else {})},
            )
            r = conn.getresponse()
            r.read()
            case.assertEqual(r.status, status)
            case.assertEqual(
                read_settings(proof.root)["validation_percent"],
                25 if status == 200 else 0,
            )
    conn.request("POST", "/api/proof/run", json.dumps(PAIR), headers)
    plain = json.load(conn.getresponse())
    case.assertEqual(plain["evaluation"]["validation_percent"], 25)
    case.assertNotIn("plateau", plain)
    conn.request(
        "POST",
        "/api/proof/optimize",
        json.dumps(PAIR),
        {**headers, "Origin": "https://example.org"},
    )
    denied = conn.getresponse()
    case.assertEqual(denied.status, 403)
    denied.read()
    conn.request("POST", "/api/proof/optimize", json.dumps(PAIR), headers)
    response = conn.getresponse()
    case.assertEqual(response.status, 200)
    result = json.loads(response.read())
    case.assertIsNotNone(result["selected_parameters"])
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
    case.assertEqual(response.status, 200)
    displayed = json.loads(response.read())
    case.assertEqual(displayed["selection"]["parameters"], result["selected_parameters"])
    case.assertEqual(displayed["plateau"], result["plateau"])
    case.assertEqual(
        result["run_id"],
        json.loads((inputs.history.parent / "proof-searches.jsonl").read_text())["run_id"],
    )


# ---- folded from the retired P7.2 shard proof_search_fixtures_p72_packages_proof_tests_test_proof_search ----
PAIR = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"}

CALENDAR = {"universe": "calendar_native", "asset": "SR", "pair": "SRH3/SRM3"}


__all__ = [
    "CALENDAR",
    "PAIR",
    "PlateauTests",
    "ProofSearchIntegrationTests",
]


class PlateauTests(unittest.TestCase):
    """Landscape, tuning and measurement regressions."""

    def test_one_axis_probes_with_constraints_missing_and_negative_results(self):
        check_one_axis_probes_with_constraints_missing_and_negative_results(self)

    def test_tpe_selects_the_highest_score_even_at_an_unstable_peak(self):
        check_tpe_selects_the_highest_score_even_at_an_unstable_peak(self)

    def test_missing_exposure_names_each_period_and_counts_a_carried_spread(self):
        check_missing_exposure_names_each_period_and_counts_a_carried_spread(self)

    def test_benchmark_relative_drawdown_and_shortfall_no_longer_veto_a_candidate(self):
        check_benchmark_relative_drawdown_and_shortfall_no_longer_veto_a_candidate(self)

    def test_mark_change_including_first_validation_session_not_exit_trade_sum(self):
        check_mark_change_including_first_validation_session_not_exit_trade_sum(self)

    def test_daily_hurdle_marks_positions_across_a_split_and_reconciles_at_exit(self):
        check_daily_hurdle_marks_positions_across_a_split_and_reconciles_at_exit(self)


class ProofSearchIntegrationTests(unittest.TestCase):
    """Research-run, settings and loopback HTTP regressions."""

    def fixture(self, builder):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        builder(root)
        return ProofService(root)

    def test_global_seed_makes_a_run_reproducible_and_zero_draws_a_new_one(self):
        check_global_seed_makes_a_run_reproducible_and_zero_draws_a_new_one(self)

    def test_pair_replay_reproducibility_and_holdout_is_not_a_selection_input(self):
        check_pair_replay_reproducibility_and_holdout_is_not_a_selection_input(self)

    def test_0_and_50_share_change_result_not_history_or_signals(self):
        check_0_and_50_share_change_result_not_history_or_signals(self)

    def test_journal_only_rows_do_not_move_the_validation_boundary(self):
        check_journal_only_rows_do_not_move_the_validation_boundary(self)

    def test_validation_liquidity_failure_does_not_veto_calendar_training_plateau(self):
        check_validation_liquidity_failure_does_not_veto_calendar_training_plateau(self)

    def test_funding_and_both_calendar_strategies_use_only_active_fields(self):
        check_funding_and_both_calendar_strategies_use_only_active_fields(self)

    def test_short_series_and_invalid_values_never_produce_a_fake_plateau(self):
        check_short_series_and_invalid_values_never_produce_a_fake_plateau(self)

    def test_http_home_alias_new_search_rerun_and_local_origin_boundary(self):
        check_http_home_alias_new_search_rerun_and_local_origin_boundary(self)


if __name__ == "__main__":
    unittest.main()
