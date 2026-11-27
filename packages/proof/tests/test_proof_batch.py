"""Fixed-cohort proof search: real disjoint fixture histories, accounting and admission.

Also covers the shared cohort preparation helpers, the report assembly they feed,
and the continuous-account annualization contracts the members are judged by.
"""

import json
import math
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.settings import EvaluationSplit, save_settings
from moex_spread_lab.core.w0 import W0Error, _run_id
from moex_spread_lab.proof import cohort_prepare as helpers
from moex_spread_lab.proof import (
    cohort_reporting,
    evaluation,
    proof_batch,
    proof_kernel,
    proof_search,
)
from moex_spread_lab.proof.landscape import Axis
from moex_spread_lab.proof.proof_batch import MAX_TARGETS, _targets
from moex_spread_lab.proof.proof_search import TRIALS, _annualized_returns, research_run
from moex_spread_lab.proof.service import ProofService
from moex_spread_lab.strategies.calendar import calendar_spreads
from proof_fixtures import fixture

P = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6"}

C = {"universe": "calendar_native", "asset": "SR", "pair": "SRH3/SRM3"}

ACTIVE = {
    **P,
    "strategy": "mean_reversion",
    "parameters": {"lookback": 10, "entry_z": 1.5, "exit_z": 0.5},
}


@pytest.fixture
def batch_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("batch-root")
    fixture(root)
    # A fixed seed keeps repeated searches comparable; seed 0 would draw a new
    # one per run and these tests compare two searches against each other.
    save_settings(root, {"random_seed": 41})
    yield root


@pytest.fixture
def service(batch_root: Path) -> Iterator[ProofService]:
    yield ProofService(batch_root)


def search(service: ProofService, base=ACTIVE, **kw):
    return research_run(service, {**base, **kw}, optimize=True)


def _manual_training_excess(service: ProofService, root: Path, row, req):
    """Independent ordinary replay cut on this member's own training end."""
    cutoff = row["evaluation"]["periods"]["training"][-1]
    prefix = service.prepare(req)(req, through=cutoff)
    dates = row["evaluation"]["periods"]["training"]
    prices = {
        r["date"]: r["price"]
        for r in json.loads((root / "data/benchmark/lqdt-lineage.json").read_text())["rows"]
    }
    wealth = 30_000.0
    last_px = prices[dates[0]]
    previous_net = 0.0
    reserved = 0
    for r in prefix["ledger"]:
        d = r["date"]
        price = prices[d]
        net = r["cumulative_net_rub"]
        wealth += (wealth - reserved) * (price / last_px - 1) + net - previous_net
        reserved = sum(
            t["margin_rub"] for t in prefix["trades"] if t["entry"]["date"] <= d < t["exit"]["date"]
        )
        last_px = price
        previous_net = net
    years = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days / 365
    return 100 * ((wealth / 30_000) ** (1 / years) - (last_px / prices[dates[0]]) ** (1 / years))


def _assert_member_replay(service: ProofService, root: Path, row, key, strategy, params):
    """The selected member replays identically and matches manual wealth math."""
    expected = {
        key: params["lookback"],
        "entry_z": params["entry_z"],
        "exit_z": params["exit_z"],
    }
    assert row["selection"]["parameters"] == expected
    assert row["selection"]["strategy"] == strategy
    req = row["selection"]
    rerun = research_run(service, req, optimize=False)
    assert rerun["run_id"] == row["run_id"]
    assert rerun["evaluation"] == row["evaluation"]
    assert row["training_annualized_excess_pp"] == pytest.approx(
        _manual_training_excess(service, root, row, req)
    )


def test_independent_cagr_minus_benchmark_and_continuous_validation_opening():
    # Hand calculation: one year between marks. 3k margin held during the
    # +10% interval then released; reserve earns no LQDT. Book earns 600 net.
    days = ["2021-01-01", "2022-01-01", "2023-01-01"]
    marks: dict[str, float] = dict(zip(days, [100.0, 110.0, 121.0], strict=True))
    proof = {
        "ledger": [
            {"date": d, "cumulative_net_rub": v} for d, v in zip(days, [0, 600, 600], strict=True)
        ],
        "trades": [
            {
                "entry": {"date": days[0]},
                "exit": {"date": days[1]},
                "margin_rub": 3000,
            }
        ],
    }
    training = _annualized_returns(proof, marks, EvaluationSplit.from_dates(days[:2], 0))
    assert training["calendar_days"] == 365
    assert training["strategy_annualized_pct"] == pytest.approx(11.0)
    assert training["lqdt_annualized_pct"] == pytest.approx(10.0)
    assert training["annualized_excess_pp"] == pytest.approx(1.0)
    final = _annualized_returns(proof, marks, EvaluationSplit.from_dates(days, 25))
    assert final["calendar_days"] == 365
    assert final["annualized_excess_pp"] == 0.0
    whole = _annualized_returns(proof, marks, EvaluationSplit.from_dates(days, 0))
    assert whole["annualized_excess_pp"] == pytest.approx(
        100 * ((33300 * 1.1 / 30000) ** 0.5 - 1.1)
    )


ANNUALIZATION_DAYS = ["2021-01-01", "2022-01-01", "2023-01-01"]


@pytest.mark.parametrize(
    ("bad_days", "bad_marks"),
    [
        (ANNUALIZATION_DAYS[:1], dict(zip(ANNUALIZATION_DAYS, [100.0, 110.0, 121.0], strict=True))),
        (ANNUALIZATION_DAYS, dict.fromkeys(ANNUALIZATION_DAYS, 0.0)),
    ],
    ids=["single-day", "zero-lqdt"],
)
def test_annualization_rejects_a_single_day_or_a_zero_benchmark(bad_days, bad_marks):
    days = ANNUALIZATION_DAYS
    proof = {
        "ledger": [
            {"date": d, "cumulative_net_rub": v} for d, v in zip(days, [0, 600, 600], strict=True)
        ],
        "trades": [
            {
                "entry": {"date": days[0]},
                "exit": {"date": days[1]},
                "margin_rub": 3000,
            }
        ],
    }
    with pytest.raises(W0Error):
        _annualized_returns(proof, bad_marks, EvaluationSplit.from_dates(bad_days, 0))


def test_one_maximum_across_disjoint_training_windows_and_both_native_spellings(
    service, batch_root
):
    root = batch_root
    save_settings(root, {"validation_percent": 25})
    with patch.object(
        calendar_spreads, "fetch_inputs", wraps=calendar_spreads.fetch_inputs
    ) as loads:
        result = search(service, targets=[P, C])
        assert loads.call_count == 1
    assert result == search(service, targets=[C, P])
    assert result["trial_count"] == TRIALS
    assert result["status"] == "maximum_found"
    g = result["group"]
    assert g["member_count"] == 2
    cal, pair = g["members"]
    assert [cal["target"], pair["target"]] == [C, P]
    assert (
        cal["evaluation"]["periods"]["validation"][-1]
        < pair["evaluation"]["periods"]["training"][0]
    )
    assert cal["evaluation"]["observations"] != pair["evaluation"]["observations"]
    scores = {}
    for t in result["candidates"]:
        assert len(t["by_window_pp"]) == 2
        assert t["rank_score"] == pytest.approx(sum(t["by_window_pp"]) / 2)
        assert (
            5 <= t["parameters"]["lookback"] <= 40
            and 0.1 <= t["parameters"]["exit_z"] < t["parameters"]["entry_z"]
        )
        scores.setdefault(t["rank_score"], []).append(t["parameters"])
    assert g["training_mean_pp"] == max(scores)
    params = result["selected_parameters"]
    # Several candidates may tie on the maximum; the winner is one of them.
    assert params in scores[max(scores)]
    assert pair["selection"]["parameters"] == params
    for row, key, strategy in [
        (cal, "window", "zscore_mean_reversion"),
        (pair, "lookback", "mean_reversion"),
    ]:
        _assert_member_replay(service, root, row, key, strategy, params)
    rates = [m["evaluation"]["result"]["annualized_excess_pp"] for m in g["members"]]
    assert g["result_mean_pp"] == pytest.approx(sum(rates) / 2)
    assert g["training_mean_pp"] != g["result_mean_pp"]
    cal_active = {
        **C,
        "strategy": "zscore_mean_reversion",
        "parameters": {"window": 10, "entry_z": 1.5, "exit_z": 0.5},
    }
    reverse = search(service, cal_active, targets=[P, C])
    assert reverse["selected_parameters"] == cal["selection"]["parameters"]
    assert reverse["group"] == g


def test_only_holdout_liquidity_changes_neither_trials_nor_winner_or_lamps(service, batch_root):
    root = batch_root
    save_settings(root, {"validation_percent": 25, "allow_failed_volume": False})
    before = search(service, targets=[P, C])
    path = root / "data/w1/candles/CRZ6_1d.json"
    data = json.loads(path.read_text())
    data["rows"][-2]["volume"] = 1
    path.write_text(json.dumps(data))
    after = search(service, targets=[P, C])
    g = after["group"]
    for k in ("status", "candidates", "selected_parameters", "plateau"):
        assert before[k] == after[k]
    assert g["member_count"] == 2
    assert g["result_mean_pp"] is None
    assert g["training_mean_pp"] is not None
    assert "ликвидности" in g["members"][1]["evaluation"]["result"]["reason"]
    save_settings(root, {"validation_percent": 0})
    blocked = search(service, targets=[P, C])
    assert blocked["remaining_count"] == 1
    assert blocked["excluded"][0]["target"] == P


def test_no_exposure_is_weighted_zero_not_a_dropped_member(service, batch_root):
    root = batch_root
    path = root / "data/w1/candles/CRZ6_1d.json"
    far = json.loads(path.read_text())
    near = json.loads((path.parent / "CNYRUBF_1d.json").read_text())
    for r, n in zip(far["rows"], near["rows"], strict=True):
        for k in ("open", "close", "high", "low"):
            r[k] = n[k]
    path.write_text(json.dumps(far))
    g = search(service, targets=[C, P])
    assert g["status"] == "maximum_found"
    for t in g["candidates"]:
        assert t["by_window_pp"][1] == 0
        assert t["rank_score"] == t["by_window_pp"][0] / 2
    m = g["group"]["members"][1]
    assert "спреда не было" in m["evaluation"]["result"]["reason"]
    assert m["training_annualized_excess_pp"] == 0
    assert g["group"]["training_mean_pp"] > 0


def test_failed_member_prunes_the_whole_proposal_not_the_denominator(service):
    original = proof_kernel._measure

    def fail_some(proof, marks, split, settings):
        if (
            proof["selection"]["strategy"] == "mean_reversion"
            and proof["selection"]["parameters"]["lookback"] > 10
        ):
            return {"utility": None, "reason": "unmeasurable member"}
        return original(proof, marks, split, settings)

    with patch.object(proof_kernel, "_measure", side_effect=fail_some):
        r = search(service, targets=[C, P])
    assert any(t["rank_score"] is None for t in r["candidates"])
    assert r["selected_parameters"]["lookback"] <= 10
    for t in r["candidates"]:
        if t["parameters"]["lookback"] > 10:
            assert t["rank_score"] is None
            assert P["pair"] in t["reason"]
    with patch.object(
        proof_kernel,
        "_measure",
        return_value={"utility": None, "reason": "unmeasurable"},
    ):
        none = search(service, targets=[C, P])
        assert none["status"] == "no_admissible_candidate"
        assert none["selected_parameters"] is None
        assert none["group"]["training_mean_pp"] is None


def test_empty_targets_return_the_focus_only_profile(service):
    single = search(service, targets=[])
    assert single == search(service)
    assert single == search(service, targets=[P])


@pytest.mark.parametrize(
    ("base", "targets", "excluded"),
    [
        ({**P, "strategy": "funding_only"}, [P, C], [C]),
        ({**P, "strategy": "funding_only"}, [C], [C]),
        ({**C, "strategy": "convergence_to_ruonia"}, [P, C], [P]),
        (
            {**C, "strategy": "convergence_to_ruonia"},
            [{**C, "asset": "RI"}],
            [{**C, "asset": "RI"}],
        ),
    ],
    ids=["funding-both", "funding-calendar-only", "ruonia-both", "ruonia-ri"],
)
def test_explicit_rejections_return_before_optuna(service, base, targets, excluded):
    with patch(
        "moex_spread_lab.proof.proof_batch._tune",
        side_effect=AssertionError("no pre-confirmation search"),
    ):
        r = search(service, base, targets=targets)
        assert r["status"] == "selection_requires_confirmation"
        assert [x["target"] for x in r["excluded"]] == excluded
        assert r["remaining_count"] == len(targets) - len(excluded)
        assert all(x["reason"] for x in r["excluded"])
        assert r["trial_count"] == 0


@pytest.mark.parametrize(
    "raw",
    [
        [P, P],
        [{**P, "parameters": {}}],
        [{**C, "asset": None}],
        None,
        [P] * (MAX_TARGETS + 1),
    ],
    ids=["duplicate", "empty-parameters", "unknown-asset", "none", "overflow"],
)
def test_target_validation_rejects_invalid_cohort_requests(raw):
    with pytest.raises(W0Error):
        _targets({**ACTIVE, "targets": raw})


def test_large_confirmation_preview_has_no_run_id_and_cancellation_aborts(service, batch_root):
    root = batch_root
    req = {
        **P,
        "strategy": "funding_only",
        "targets": [P, *[{**C, "pair": f"W{i}/T{i}"} for i in range(200)]],
    }
    assert len(json.dumps(req)) > 8192
    result = research_run(service, req, optimize=True)
    assert (len(result["excluded"]), result["remaining_count"]) == (200, 1)
    assert result.get("run_id") is None
    assert not (root / "data/analytics/proof-searches.jsonl").exists()
    with pytest.raises(ConnectionAbortedError):
        research_run(
            service,
            ACTIVE,
            optimize=True,
            check_cancelled=lambda: (_ for _ in ()).throw(ConnectionAbortedError()),
        )


# ---- shared cohort preparation helpers (folded into the admission suite) ----


def test_cohort_request_helpers_remain_reexported():
    # proof_batch keeps only the names it consumes; the rest are helpers-private.
    for name in ("MAX_TARGETS", "_targets"):
        assert getattr(proof_batch, name) is getattr(helpers, name)


def test_target_sorting_duplicates_and_strategy_translation():
    pair_a = {"universe": "pair_screen", "pair": "A/B"}
    pair_z = {"universe": "pair_screen", "pair": "Z/Y"}
    assert helpers._targets({"targets": [pair_z, pair_a]}) == [pair_a, pair_z]
    with pytest.raises(W0Error, match="дважды"):
        helpers._targets({"targets": [pair_a, pair_a]})
    assert helpers._translate({"window": 12, "entry_z": 1.5}, "mean_reversion") == {
        "lookback": 12,
        "entry_z": 1.5,
    }
    assert helpers._translate({"lookback": 9}, "zscore_mean_reversion") == {"window": 9}


def test_intersection_rejection_and_confirmation_preserve_denominator():
    axes = {"lookback": Axis(5, 50, 1)}
    helpers._intersect_axes(axes, {"window": Axis(7, 40, 1)}, "mean_reversion")
    assert axes["lookback"] == Axis(7, 40, 1)
    short_target, valid_target = {"pair": "A/B"}, {"pair": "C/D"}
    short = SimpleNamespace(split=SimpleNamespace(training=list(range(8))))
    valid = SimpleNamespace(split=SimpleNamespace(training=list(range(9))))
    rejected = [{"target": {"pair": "E/F"}, "reason": "earlier"}]
    members = helpers._filter_training(
        cast(Any, [(short_target, short), (valid_target, valid)]), axes, rejected
    )
    assert members == [(valid_target, valid)]
    assert rejected[1] == {
        "target": short_target,
        "reason": "Для общего окна сигнала нужно не менее 9 наблюдений на обучении.",
    }
    settings = SimpleNamespace(metadata=lambda: {"validation_percent": 25})
    assert helpers._confirmation(cast(Any, settings), rejected, members) == {
        "status": "selection_requires_confirmation",
        "validation_percent": 25,
        "excluded": rejected,
        "remaining_count": 1,
        "trial_count": 0,
        "selected_parameters": None,
    }


# ---- cohort report assembly (folded into the admission suite) ----


def test_report_keeps_run_id_progress_and_single_window_details():
    point = {"window": 10}
    evaluation_row = {"result": {"annualized_excess_pp": 3.0}}
    first = SimpleNamespace(request={"strategy": "x"}, full={"run_id": "input"})
    rows = [{"target": {"pair": "A/B"}, "evaluation": evaluation_row}]
    single = {"run_id": "output", "evaluation": evaluation_row}
    land = SimpleNamespace(measure=Mock(return_value={"rank_score": 3.0, "utility": 3.0}))

    def report_members(point_arg, best, settings, check_cancelled, *, on_progress):
        assert point_arg == best == point
        on_progress(1, 1)
        return rows, False, single

    def plateau(point_arg, *, on_progress):
        assert point_arg == point
        on_progress(1, 1)
        return {"green": 70}

    group = SimpleNamespace(
        axes={"window": object()},
        seed=point,
        members=[({}, first)],
        land=land,
        active="x",
        report_members=report_members,
        plateau=plateau,
    )
    settings = SimpleNamespace(
        random_seed=41, validation_percent=25, metadata=lambda: {"validation_percent": 25}
    )
    events = []
    result = proof_batch._cohort_result(
        {"strategy": "x", "targets": [{}]},
        settings,
        group,
        point,
        1,
        [point],
        lambda: None,
        on_progress=events.append,
    )
    assert result["selection"] == {"strategy": "x"}
    assert result["group"]["result_mean_pp"] == 3.0
    assert result["input_run_id"] == "input" and result["result_run_id"] == "output"
    assert result["plateau"] == {"green": 70}
    assert [e["percent"] for e in events] == [88.0, 94.0, 94.0, 99.0]
    assert result["run_id"] == _run_id({k: v for k, v in result.items() if k != "run_id"})


def test_disabled_plateau_has_no_report_side_effects():
    group = SimpleNamespace(plateau=Mock())
    assert cohort_reporting._report_plateau(group, {}, False, None) is None
    group.plateau.assert_not_called()


# ---- continuous-account annualization (folded into the admission suite) ----


def test_evaluation_exports_remain_compatible():
    for name in ("_annualized_returns", "_excess_curve", "account_checks"):
        assert getattr(proof_kernel, name) is getattr(evaluation, name.lstrip("_"))
        assert getattr(proof_search, name) is getattr(evaluation, name.lstrip("_"))


def test_validation_uses_continuous_automatic_account_and_reports_shortfall():
    days = ["2021-01-01", "2022-01-01", "2023-01-01"]
    proof = {
        "selection": {"sizing": AUTO_SIZING},
        "sizing": {"initial_capital_rub": 100},
        "ledger": [
            {
                "date": day,
                "account_value_rub": value,
                "cash_shortfall": shortfall,
                "free_cash_rub": free,
            }
            for day, value, shortfall, free in zip(
                days, [100, 120, 132], [False, True, False], [20, -5, 15], strict=True
            )
        ],
        "trades": [],
    }
    marks = dict(zip(days, [100.0, 110.0, 121.0], strict=True))
    split = EvaluationSplit.from_dates(days, 25)
    result = evaluation.annualized_returns(proof, marks, split)
    assert result["calendar_days"] == 365
    assert result["strategy_annualized_pct"] == pytest.approx(10)
    assert result["lqdt_annualized_pct"] == pytest.approx(10)
    assert result["annualized_excess_pp"] == 0
    assert result["cash_shortfall"] is True
    assert evaluation.account_checks(proof) == {
        "cash_shortfall_rows": 1,
        "first_cash_shortfall_date": days[1],
        "minimum_free_cash_rub": -5,
    }
    for invalid in (0, math.inf, math.nan):
        with pytest.raises(W0Error, match="LQDT"):
            evaluation.annualized_returns(proof, {**marks, days[1]: invalid}, split)
