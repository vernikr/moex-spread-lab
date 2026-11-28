"""Proof-search regressions: landscape, research runs and loopback boundary."""

import json
from collections.abc import Iterator

import pytest

from moex_spread_lab.core.settings import (
    EvaluationSplit,
    ResearchSettings,
    read_settings,
    save_settings,
)
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.proof_kernel import TuningOptions
from moex_spread_lab.proof.proof_search import (
    EXECUTION_AXES,
    GREEN,
    TRIALS,
    Axis,
    Landscape,
    StudyWindow,
    _excess_curve,
    _measure,
    _tune,
    research_run,
)
from moex_spread_lab.proof.service import ProofService, benchmark_marks
from moex_spread_lab.proof.strategy_catalog import get_strategy_axes
from workspace_fixtures import build_calendar_root, build_pair_root

PAIR = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"}

CALENDAR = {"universe": "calendar_native", "asset": "SR", "pair": "SRH3/SRM3"}


def _funding_proof(days, ledger, *, trades: bool | list[dict[str, object]] = True, sizing=None):
    """Stub ``funding_only`` proof dict shared by the ``_measure`` unit tests."""
    if trades is True:
        trades = [
            {
                "entry": {"date": days[0]},
                "exit": {"date": days[-1]},
                "margin_rub": 100,
                "lqdt_on_margin_rub": 4,
            }
        ]
    elif trades is False:
        trades = []
    proof = {
        "selection": {"strategy": "funding_only"},
        "window": {"passes_capital_screen": True, "tradable_every_day": True},
        "ledger": [
            {"date": d, "cumulative_net_rub": x} for d, x in zip(days, ledger, strict=False)
        ],
        "trades": trades,
    }
    if sizing is not None:
        proof["sizing"] = sizing
    return proof


def _measure_funding(days, ledger, percent, *, trades=True, sizing=None, marks=None):
    if marks is None:
        marks = dict.fromkeys(days, 1.0)
    return _measure(
        _funding_proof(days, ledger, trades=trades, sizing=sizing),
        marks,
        EvaluationSplit.from_dates(days, percent),
        ResearchSettings(),
    )


@pytest.fixture
def pair_service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ProofService]:
    root = tmp_path_factory.mktemp("pair-search")
    build_pair_root(root)
    yield ProofService(root)


@pytest.fixture
def calendar_service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ProofService]:
    root = tmp_path_factory.mktemp("calendar-search")
    build_calendar_root(root)
    yield ProofService(root)


def test_global_seed_makes_a_run_reproducible_and_zero_draws_a_new_one(pair_service):
    proof = pair_service
    req = {**PAIR, "slippage_ticks": 0.5}
    # A fixed seed reproduces the whole search, proposal by proposal.
    save_settings(proof.root, {"random_seed": 41})
    first = research_run(proof, req, optimize=True)
    assert first["seed"] == 41
    assert first["seed_used"] == 41
    again = research_run(proof, req, optimize=True)
    assert first["candidates"] == again["candidates"]
    assert first["selected_parameters"] == again["selected_parameters"]
    # Seed 0 draws a fresh seed per run, so the search is not pinned.
    save_settings(proof.root, {"random_seed": 0})
    drawn = [research_run(proof, req, optimize=True) for _ in range(3)]
    for run in drawn:
        assert run["seed"] == 0
        assert run["seed_used"] > 0
    assert len({run["seed_used"] for run in drawn}) > 1
    # The reported seed_used is the base seed that drove the sampler, so
    # pinning it replays the run candidate by candidate (issue #290).
    save_settings(proof.root, {"random_seed": drawn[0]["seed_used"]})
    replay = research_run(proof, req, optimize=True)
    assert replay["seed_used"] == drawn[0]["seed_used"]
    assert replay["candidates"] == drawn[0]["candidates"]
    assert replay["selected_parameters"] == drawn[0]["selected_parameters"]


def test_idle_book_on_a_ledger_starting_before_training_scores_exactly_zero(tmp_path):
    # Leading zero-volume rows keep the account in LQDT while the first
    # volume-eligible training day arrives later: an idle book must contribute
    # zero excess, not a free positive bias (issue #303).
    root = tmp_path
    build_pair_root(root)
    candles = root / "data/w1/candles/CRZ6_1d.json"
    data = json.loads(candles.read_text())
    for row in data["rows"][:6]:
        row["volume"] = 0
    candles.write_text(json.dumps(data))
    service = ProofService(root)
    save_settings(root, {"random_seed": 41})
    settings = ResearchSettings.load(root)
    req = {
        **PAIR,
        "slippage_ticks": 0.5,
        "sizing": "max_affordable",
        "parameters": {"lookback": 10, "entry_z": 5.0, "exit_z": 1.0},
    }
    benchmark = benchmark_marks(root / "data/benchmark/lqdt-lineage.json")
    case = StudyWindow(service, req, settings, benchmark)
    idle = {"lookback": 10, "entry_z": 5.0, "exit_z": 1.0}
    report = case.report(idle)
    assert report["trades"] == []
    assert case.split.training[0] > report["ledger"][0]["date"]
    result = report["evaluation"]["result"]
    assert result["excess_rub"] == 0.0
    assert result["annualized_excess_pp"] == 0.0


def test_pair_replay_reproducibility_and_holdout_is_not_a_selection_input(pair_service):
    proof = pair_service
    # A fixed seed keeps two runs comparable; seed 0 would draw a new one.
    save_settings(proof.root, {"validation_percent": 25, "random_seed": 41})
    req = {**PAIR, "slippage_ticks": 0.5}
    a = research_run(proof, req, optimize=True)
    assert a == research_run(proof, req, optimize=True)
    assert a["method"] == "Optuna TPESampler"
    assert a["trial_count"] == TRIALS
    assert [c["trial"] for c in a["candidates"]] == list(range(TRIALS))
    assert a["status"] == "maximum_found"
    assert set(a["selected_parameters"]) == {"lookback", "entry_z", "exit_z"}
    picked = {**req, "parameters": a["selected_parameters"]}
    displayed = research_run(proof, picked, optimize=False)
    assert displayed["plateau"] == a["plateau"]
    assert displayed["evaluation"] == a["evaluation"]
    assert displayed["run_id"] == a["result_run_id"]
    assert displayed["proof_run_id"] == proof.proof(picked)["run_id"]
    assert set(a["evaluation"]["periods"]) == {"training", "validation"}
    start = a["evaluation"]["periods"]["validation"][0]
    path = proof.root / "data/w1/candles/CRZ6_1d.json"
    data = json.loads(path.read_text())
    for row in data["rows"]:
        if row["begin"][:10] >= start:
            for key in ("open", "high", "low", "close"):
                row[key] += 4
    path.write_text(json.dumps(data))
    changed = research_run(proof, req, optimize=True)
    assert a["input_run_id"] != changed["input_run_id"]
    assert a["evaluation"]["result"]["excess_rub"] != changed["evaluation"]["result"]["excess_rub"]
    for key in ("selected_parameters", "candidates", "plateau"):
        assert a[key] == changed[key]


@pytest.mark.parametrize(
    ("fixture_name", "req"),
    [
        ("pair_service", {**PAIR, "parameters": {"lookback": 10, "entry_z": 1, "exit_z": 0.5}}),
        ("calendar_service", {**CALENDAR, "strategy": "zscore_mean_reversion"}),
    ],
    ids=["pair", "calendar"],
)
def test_0_and_50_share_change_result_not_history_or_signals(request, fixture_name, req):
    service = request.getfixturevalue(fixture_name)
    whole = research_run(service, req, optimize=False, include_plateau=False)
    assert "plateau" not in whole
    e = whole["evaluation"]
    assert e["result_basis"] == "training"
    assert e["periods"]["validation"] is None
    assert e["observations"]["validation"] == 0
    assert e["result"]["net_rub"] == pytest.approx(whole["summary"]["net_rub"], abs=0.03)
    save_settings(service.root, {"validation_percent": 50})
    half = research_run(service, req, optimize=False, include_plateau=False)
    for k in ("proof_run_id", "series", "trades", "ledger", "summary"):
        assert whole[k] == half[k]
    assert whole["run_id"] != half["run_id"]
    h = half["evaluation"]
    assert h["result_basis"] == "validation"
    assert h["observations"]["validation"] == e["observations"]["training"] // 2
    marks = {r["date"]: r["cumulative_net_rub"] for r in half["ledger"]}
    assert h["result"]["net_rub"] == pytest.approx(
        marks[h["periods"]["validation"][-1]] - marks[h["periods"]["training"][-1]]
    )


def test_journal_only_rows_do_not_move_the_validation_boundary(pair_service):
    service = pair_service
    save_settings(service.root, {"validation_percent": 50})
    path = service.root / "data/w1/candles/CRZ6_1d.json"
    data = json.loads(path.read_text())
    for i in (0, 27, -1):
        data["rows"][i]["volume"] = 0
    path.write_text(json.dumps(data))
    result = research_run(service, PAIR, optimize=False)
    e = result["evaluation"]
    assert len(result["ledger"]) == 55
    assert e["observations"] == {"training": 26, "validation": 26}
    assert e["periods"]["validation"] == [
        data["rows"][28]["begin"][:10],
        data["rows"][-2]["begin"][:10],
    ]


def test_validation_liquidity_failure_does_not_veto_calendar_training_plateau(calendar_service):
    service = calendar_service
    save_settings(
        service.root,
        # A fixed seed keeps the two runs comparable; seed 0 would draw a new one.
        {"validation_percent": 25, "allow_failed_volume": False, "random_seed": 41},
    )
    req = {**CALENDAR, "strategy": "zscore_mean_reversion"}
    before = research_run(service, req, optimize=True)
    assert before["selected_parameters"] is not None
    path = next((service.root / "data/spread-history-cache").glob("SRH3SRM3-*.json"))
    data = json.loads(path.read_text())
    history = data["payload"]["history"]
    dates, volume = (history["columns"].index(k) for k in ("TRADEDATE", "VOLUME"))
    start = before["evaluation"]["periods"]["validation"][0]
    next(row for row in history["data"] if row[dates] == start)[volume] = 1
    path.write_text(json.dumps(data))
    after = research_run(service, req, optimize=True)
    for k in ("status", "selected_parameters", "candidates", "plateau"):
        assert before[k] == after[k]
    assert "ликвидности" in after["evaluation"]["result"]["reason"]


@pytest.mark.parametrize(
    ("fixture_name", "req", "active"),
    [
        ("pair_service", {**PAIR, "strategy": "funding_only"}, {"lookback"}),
        (
            "calendar_service",
            {**CALENDAR, "strategy": "zscore_mean_reversion"},
            {"window", "entry_z", "exit_z"},
        ),
        (
            "calendar_service",
            {**CALENDAR, "strategy": "convergence_to_ruonia"},
            {"threshold_pp"},
        ),
    ],
    ids=["funding", "zscore", "ruonia"],
)
def test_funding_and_both_calendar_strategies_use_only_active_fields(
    request, fixture_name, active, req
):
    service = request.getfixturevalue(fixture_name)
    result = research_run(service, req, optimize=True)
    json.dumps(result, allow_nan=False)
    assert result["trial_count"] == TRIALS
    assert set(result["plateau"]["fields"]) == active | EXECUTION_AXES.keys()
    assert all(set(t["parameters"]) == active for t in result["candidates"])


def test_capital_screen_waiver_still_gates_the_calendar_book(calendar_service):
    cal = calendar_service
    save_settings(cal.root, {"allow_failed_capital": False})
    no_capital = research_run(cal, {**CALENDAR, "quantity": 3}, optimize=True)
    assert no_capital["status"] == "unavailable"
    assert no_capital["trial_count"] == 0
    assert no_capital["selected_parameters"] is None
    assert all(f["score"] is None for f in no_capital["plateau"]["fields"].values())


def test_short_series_and_invalid_values_never_produce_a_fake_plateau(pair_service):
    proof = pair_service
    path = proof.root / "data/w1/candles/CRZ6_1d.json"
    data = json.loads(path.read_text())
    data["rows"] = data["rows"][:18]
    path.write_text(json.dumps(data))
    result = research_run(proof, PAIR, optimize=True)
    assert result["selected_parameters"] is not None
    for lamp in result["plateau"]["fields"].values():
        assert lamp["score"] is None
        assert "18" in lamp["reason"]
    data["rows"] = data["rows"][:5]
    path.write_text(json.dumps(data))
    too_short = research_run(proof, PAIR, optimize=True)
    assert too_short["status"] == "unavailable"
    assert too_short["selected_parameters"] is None
    for params in (
        {"lookback": 6.5},
        {"lookback": True},
        {"entry_z": 1, "exit_z": 2},
        {"exit_z": float("nan")},
        {"threshold_pp": 1},
    ):
        with pytest.raises(W0Error):
            research_run(proof, {**PAIR, "parameters": params}, optimize=True)


def test_one_axis_probes_with_constraints_missing_and_negative_results():
    axes = get_strategy_axes()["mean_reversion"] | EXECUTION_AXES
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
        assert p["exit_z"] < p["entry_z"]
        if p["quantity"] > 1:
            raise W0Error("capital")
        return {"utility": 100 - 20 * abs(p["lookback"] - 10)}

    land = Landscape(axes, evaluate)
    window = land.profile(seed, "lookback")
    assert window["score"] == 60
    assert [n["value"] for n in window["neighbors"]] == [8, 9, 11, 12]
    assert all(p["entry_z"] == 1.0 for p in seen)
    entry = land.profile(seed, "entry_z")
    assert entry["score"] == 100
    assert [n["value"] for n in entry["neighbors"]] == [1.1, 1.2]
    blocked = land.profile(seed, "quantity")
    assert blocked["score"] is None
    assert all(n["reason"] == "capital" for n in blocked["neighbors"])
    negative = Landscape({"window": Axis(3, 60, 1)}, lambda _: {"utility": -2})
    assert negative.profile({"window": 20}, "window")["score"] == 0


def test_tpe_selects_the_highest_score_even_at_an_unstable_peak():
    axes = get_strategy_axes()["funding_only"]
    seed = {"lookback": 28, "quantity": 1, "slippage_ticks": 1.5}

    def objective(p):
        assert (p["quantity"], p["slippage_ticks"]) == (1, 1.5)
        v = p["lookback"]
        return {"utility": 200 if v == 28 else 70 if 8 <= v <= 23 else -1}

    land = Landscape(axes | EXECUTION_AXES, objective)
    chosen, tries, _ = _tune(land, seed, axes, 100, TuningOptions(TRIALS, None, None, None))
    assert tries == TRIALS
    assert chosen is not None
    assert chosen["lookback"] == 28
    assert land.profile(chosen, "lookback")["score"] < GREEN
    flat = Landscape(axes | EXECUTION_AXES, lambda _: {"utility": 0})
    assert _tune(flat, seed, axes, 100, TuningOptions(TRIALS, None, None, None))[0] == seed
    losing = Landscape(axes | EXECUTION_AXES, lambda p: {"utility": -1 - abs(28 - p["lookback"])})
    assert _tune(losing, seed, axes, 100, TuningOptions(TRIALS, None, None, None))[0] == seed


@pytest.mark.parametrize(
    ("span", "missing"),
    [
        ((5, 6), "только на обучении"),
        ((1, 4), "только на проверке"),
        (None, "ни на обучении, ни на проверке"),
        ((4, 5), None),  # Enter on the last training date, exit on the first validation date.
    ],
    ids=["training-only", "validation-only", "never-held", "carried"],
)
def test_missing_exposure_names_each_period_and_counts_a_carried_spread(span, missing):
    dates = [f"2026-11-{day:02}" for day in range(1, 9)]
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
    proof = _funding_proof(dates, [0] * len(dates), trades=trades)
    measured = _measure(
        proof, dict.fromkeys(dates, 1.0), EvaluationSplit.from_dates(dates, 50), ResearchSettings()
    )
    if missing is None:
        assert measured["utility"] == 0  # Measured flat result, not missing evidence.
        assert "reason" not in measured
    else:
        assert measured["utility"] is None
        assert measured["reason"] == f"Открытого спреда не было {missing}."


@pytest.mark.parametrize(
    "end_wealth",
    [30096.0, 30100.0, 30090.0],
    ids=["idle", "beats-lqdt", "loses-to-lqdt"],
)
def test_scalar_train_summary_anchors_both_legs_at_the_training_start(end_wealth):
    # The C metrics-only summary carries the wealth on the first training day,
    # so the ruble excess and the annualized score agree in sign, and an idle
    # book that already compounded in LQDT before training scores exactly 0
    # (issue #303).
    start, end = "2025-06-25", "2025-09-01"
    marks = {start: 1.0024, end: 1.0032}
    initial, p0 = 30000.0, 1.0
    proof = {
        "selection": {"strategy": "funding_only"},
        "window": {"passes_capital_screen": True, "tradable_every_day": True},
        "_scalar_train_summary": (
            initial,
            p0,
            marks[end],
            end_wealth,
            0.0,
            initial * marks[start] / p0,
            False,
            False,
        ),
    }
    measured = _measure(
        proof, marks, EvaluationSplit.from_dates([start, end], 0), ResearchSettings()
    )
    if end_wealth == 30096.0:
        assert measured["excess_rub"] == 0.0
        assert measured["annualized_excess_pp"] == 0.0
        assert measured["rank_score"] == 0.0
    else:
        assert (measured["excess_rub"] > 0) == (end_wealth > 30096.0)
        assert (measured["annualized_excess_pp"] > 0) == (end_wealth > 30096.0)


def test_benchmark_relative_drawdown_and_shortfall_no_longer_veto_a_candidate():
    # A book that falls far behind LQDT stays a candidate: the measured
    # excess is the score, and the risk path stays visible in the proof.
    # The account stays solvent, so the annualization guard still applies.
    days = [f"2026-01-0{i}" for i in range(1, 5)]
    measured = _measure_funding(
        days, [0, -12_000, -16_000, -18_000], 0, sizing={"initial_capital_rub": 100_000.0}
    )
    assert measured["rank_score"] is not None
    assert measured["utility"] is not None
    assert "просадки" not in str(measured.get("reason", ""))
    assert "отставания" not in str(measured.get("reason", ""))


@pytest.mark.parametrize(
    ("percent", "expected"),
    [(0, (72, 68, 17)), (50, (62, 59, 29.5)), (1, (2, 0, 0))],
    ids=["all-training", "half", "one-session"],
)
def test_mark_change_including_first_validation_session_not_exit_trade_sum(percent, expected):
    days = [f"2026-01-0{i}" for i in range(1, 5)]
    m = _measure_funding(
        days,
        [0, 10, 70, 72],
        percent,
        marks=dict(zip(days, (1, 1.01, 1.02, 1.04), strict=False)),
    )
    assert (m["net_rub"], m["excess_rub"], m["rub_per_session"]) == expected


def test_mark_change_without_trades_vetoes_the_split():
    days = [f"2026-01-0{i}" for i in range(1, 5)]
    no_check = _measure_funding(
        days,
        [0, 10, 70, 72],
        0,
        trades=False,
        marks=dict(zip(days, (1, 1.01, 1.02, 1.04), strict=False)),
    )
    assert no_check["reason"] == "Открытого спреда не было на обучении."
    assert no_check["validation"] is None


def test_daily_hurdle_marks_positions_across_a_split_and_reconciles_at_exit():
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
    assert curve["b"] == pytest.approx(9)
    assert curve["d"] == pytest.approx(31 - 3.33)


@pytest.mark.parametrize("bad", [51, -1, True, "25"], ids=["over", "negative", "bool", "str"])
def test_http_home_alias_new_search_rerun_and_local_origin_boundary(pair_service, bad):
    proof = pair_service
    assert read_settings(proof.root) == {
        k: v for k, v in ResearchSettings().metadata().items() if k != "schema"
    }
    with pytest.raises(W0Error):
        save_settings(proof.root, {"validation_percent": bad})
    assert read_settings(proof.root)["validation_percent"] == 0
    save_settings(proof.root, {"validation_percent": 25})
    plain = research_run(proof, dict(PAIR), optimize=False, include_plateau=False)
    assert plain["evaluation"]["validation_percent"] == 25
    assert "plateau" not in plain
    result = research_run(proof, dict(PAIR), optimize=True)
    assert result["selected_parameters"] is not None
    displayed = research_run(
        proof,
        {**PAIR, "parameters": result["selected_parameters"]},
        optimize=False,
        include_plateau=True,
    )
    assert displayed["selection"]["parameters"] == result["selected_parameters"]
    assert displayed["plateau"] == result["plateau"]
