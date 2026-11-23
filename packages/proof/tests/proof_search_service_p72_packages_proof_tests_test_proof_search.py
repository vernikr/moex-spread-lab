"""Research-run integration checks (P7.2 leaf #131).

Bodies are extracted verbatim from ``test_proof_search.py``; ``case`` is the
owning ``ProofSearchIntegrationTests`` instance, so its fixture and every
assertion keep their original behaviour.
"""

import json

from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime.proof_search import EXECUTION_AXES, TRIALS, research_run
from proof_search_fixtures_p72_packages_proof_tests_test_proof_search import CALENDAR, PAIR
from test_calendar_proof import _build_root as calendar_fixture
from test_execution_proof import _build_root as pair_fixture


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
