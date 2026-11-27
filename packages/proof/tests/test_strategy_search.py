"""Strategy choice is train-only, fixed-cohort and explicit about native execution."""

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from moex_spread_lab.accounting.position_sizing import size_proof
from moex_spread_lab.core.settings import (
    EvaluationSplit,
    ResearchSettings,
    save_settings,
)
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof import proof_kernel
from moex_spread_lab.proof.pair_payload import TRIALS_PER_STRATEGY
from moex_spread_lab.proof.proof_kernel import StudyWindow
from moex_spread_lab.proof.proof_search import _measure, _tune, _validate, research_run
from moex_spread_lab.proof.proof_windows import compact_result
from moex_spread_lab.proof.service import ProofService
from proof_fixtures import funding_winner, rising_calendar, screen_failures
from test_global_capital import FLAT, PAIR, TARGETS
from test_position_sizing import unit_proof


@pytest.fixture
def funded_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("strategy-choice")
    funding_winner(root)
    # A fixed seed keeps repeated searches comparable; seed 0 would draw a new
    # one per run and these tests compare two searches against each other.
    save_settings(root, {"random_seed": 41})
    yield root


@pytest.fixture
def service(funded_root: Path) -> Iterator[ProofService]:
    yield ProofService(funded_root)


def search(service: ProofService, request=PAIR, **extra):
    """One optimize_strategy search on the shared funded synthetic root."""
    return research_run(service, {**request, "optimize_strategy": True, **extra}, optimize=True)


def test_cross_family_tie_retains_current_and_only_winner_gets_a_report(service):
    reports = []
    # proof_search.StudyWindow is the proof_kernel class re-exported as a
    # Callable type; patch the canonical class so the attribute access types.
    original_report = proof_kernel.StudyWindow.report

    def flat(*args):
        m = _measure(*args)
        return {**m, "rank_score": 0.0, "utility": 0.0}

    def report(case, point):
        reports.append(case.request["strategy"])
        return original_report(case, point)

    with (
        patch.object(proof_kernel, "_measure", side_effect=flat),
        patch.object(proof_kernel.StudyWindow, "report", report),
    ):
        r = search(service, {**PAIR, "strategy": "funding_only", "parameters": {"lookback": 8}})
    assert r["selected_strategy"] == "funding_only"
    assert reports == ["funding_only"]


def test_holdout_changes_do_not_choose_a_different_strategy_or_trial_sequence(service, funded_root):
    save_settings(funded_root, {"validation_percent": 25, "allow_failed_volume": False})
    before = search(service)
    path = funded_root / "data/w1/candles/CRZ6_1d.json"
    data = json.loads(path.read_text())
    data["rows"][-1]["volume"] = 1
    data["rows"][-1]["close"] += 1
    path.write_text(json.dumps(data))
    # Catastrophically bad *validation* funding must not change which family wins
    # on training, nor leak into its fee reference or parameter proposals.
    path = next((funded_root / "data/carry-history-cache").glob("CNYRUBF-*.json"))
    data = json.loads(path.read_text())
    block = data["payload"]["history"]
    for row in block["data"][-12:]:
        row[block["columns"].index("SWAPRATE")] = -5.0
    path.write_text(json.dumps(data))
    after = search(service)
    for key in (
        "candidates",
        "selected_strategy",
        "selected_parameters",
        "plateau",
        "strategy_search",
    ):
        assert before[key] == after[key]
    assert before["group"]["training_mean_pp"] == after["group"]["training_mean_pp"]
    assert after["selected_strategy"] == "funding_only"
    assert after["group"]["result_mean_pp"] is None
    assert after["group"]["member_count"] == 1


def test_both_screen_permissions_still_gate_every_family_on_the_same_fixed_cohort(
    funded_root,
):
    root = funded_root / "screens"
    screen_failures(root)
    screens = ProofService(root)
    for volume, capital in (
        (True, True),
        (False, True),
        (True, False),
        (False, False),
    ):
        save_settings(root, {"allow_failed_volume": volume, "allow_failed_capital": capital})
        result = search(screens, targets=TARGETS)
        excluded = {
            t["pair"] for t, allow in ((TARGETS[0], volume), (TARGETS[1], capital)) if not allow
        }
        if excluded:
            assert result["status"] == "selection_requires_confirmation"
            assert {e["target"]["pair"] for e in result["excluded"]} == excluded
            assert result["trial_count"] == 0
            assert result["remaining_count"] == 2 - len(excluded)
        else:
            assert result["status"] == "maximum_found"
            assert result["group"]["member_count"] == 2
            assert result["selected_strategy"] == "mean_reversion"


def test_one_capital_split_slippage_snapshot_for_every_family_and_trial(service, funded_root):
    save_settings(
        funded_root,
        {
            "initial_capital_rub": 15000,
            "slippage_ticks": 2.5,
            "validation_percent": 25,
        },
    )
    seen = []

    def change(*args, **kwargs):
        save_settings(
            funded_root,
            {
                "initial_capital_rub": 60000,
                "slippage_ticks": 0,
                "validation_percent": 0,
            },
        )
        seen.append(1)
        return _tune(*args, **kwargs)

    with patch("moex_spread_lab.proof.proof_strategy_search._tune", side_effect=change):
        r = search(service)
    assert len(seen) == 2
    for member in r["group"]["members"]:
        assert member["selection"]["initial_capital_rub"] == 15000
        assert member["selection"]["slippage_ticks"] == 2.5
        assert member["evaluation"]["validation_percent"] == 25
    assert all(
        "initial_capital_rub" not in c["parameters"] and "slippage_ticks" not in c["parameters"]
        for c in r["candidates"]
    )


def test_cancellation_aborts_the_strategy_search(service):
    def cancel():
        raise ConnectionAbortedError("cancelled")

    with pytest.raises(ConnectionAbortedError):
        research_run(
            service, {**PAIR, "optimize_strategy": True}, optimize=True, check_cancelled=cancel
        )


@pytest.mark.parametrize("value", [1, 0, "true", None, []], ids=lambda v: repr(v))
def test_invalid_optimize_strategy_values_fail_closed(service, value):
    with pytest.raises(W0Error):
        search(service, optimize_strategy=value)


def test_invalid_fill_and_price_fill_types_fail_closed(service):
    with pytest.raises(W0Error):
        search(service, fill="not-a-fill")
    for value in ("same_day_settlement", "oops", None):
        with pytest.raises(W0Error):
            search(service, price_fill=value)


def test_strategy_optimization_requires_optimize_true(service):
    with pytest.raises(W0Error):
        research_run(service, {**PAIR, "optimize_strategy": True}, optimize=False)
    _validate({**PAIR, "optimize_strategy": False})


def test_strategy_search_records_every_family_and_returns_run_id_only_for_winner(service):
    result = search(service)
    assert result["selected_strategy"] == "funding_only"
    assert result.get("run_id")
    assert {item["strategy"] for item in result["strategy_search"]["alternatives"]} == {
        "mean_reversion",
        "funding_only",
    }


def test_entered_trade_can_later_have_an_unfundable_mark_never_a_zero_rate():
    proof, marks, contracts = unit_proof(near=(50000, 48000, 48000, 48000), entries=((0, 3),))
    proof = size_proof(proof, marks, ResearchSettings(), contracts)
    assert len(proof["trades"]) == 1
    assert proof["summary"]["cash_shortfall_rows"] > 0
    proof["instrument"]["family"] = "quarterly_same_asset"
    proof["window"]["tradable_every_day"] = True
    case = object.__new__(StudyWindow)
    case.settings = ResearchSettings()
    case.split = EvaluationSplit.from_dates(marks, 0)
    case.marks = marks
    case.proof = lambda point, training: proof
    report = case.report({})
    result = compact_result(report)
    assert result["trades"] == 1
    assert result["annualized_excess_pp"] is None
    evidence = result["evaluation"]["account_checks"]
    assert evidence["first_cash_shortfall_date"] == list(marks)[1]
    assert evidence["minimum_free_cash_rub"] < 0
    assert "Свободные средства" in result["evaluation"]["result"]["reason"]


def test_funding_can_replace_mr_with_a_full_budget_for_each_and_native_execution(service):
    r = search(service, fill="reference_same_bar", price_fill="reference_same_bar")
    assert r["selected_strategy"] == "funding_only"
    assert r["selected_fill"] == "same_day_settlement"
    assert r["schema_version"] == "proof-search.v4"
    assert r["trial_count"] == 2 * TRIALS_PER_STRATEGY
    assert len(r["candidates"]) == r["trial_count"]
    assert [c["trial"] for c in r["candidates"]] == list(range(r["trial_count"]))
    assert {a["strategy"] for a in r["strategy_search"]["alternatives"]} == {
        "mean_reversion",
        "funding_only",
    }
    for a in r["strategy_search"]["alternatives"]:
        records = [v for v in r["candidates"] if v["strategy"] == a["strategy"]]
        assert a["trial_count"] == TRIALS_PER_STRATEGY
        assert len(records) == TRIALS_PER_STRATEGY
        assert a["training_best_pp"] == max(
            v["rank_score"] for v in records if v["rank_score"] is not None
        )
        assert {v["fill"] for v in records} == {
            "same_day_settlement" if a["strategy"] == "funding_only" else "reference_same_bar"
        }
        assert all(
            set(v["parameters"])
            == (
                {"lookback"}
                if a["strategy"] == "funding_only"
                else {"lookback", "entry_z", "exit_z"}
            )
            for v in records
        )
    row = r["group"]["members"][0]
    assert row["selection"]["strategy"] == "funding_only"
    assert not r["group"]["execution_reference"]
    p = research_run(service, row["selection"], optimize=False, include_plateau=False)
    assert p["run_id"] == row["run_id"]
    assert p["evaluation"] == row["evaluation"]
    assert search(service, fill="reference_same_bar", price_fill="reference_same_bar") == r


def test_native_convergence_can_replace_mr_instead_of_just_being_enumerated(service, funded_root):
    rising_calendar(funded_root)
    r = search(service, FLAT, fill="next_close")
    assert r["selected_strategy"] == "convergence_to_ruonia"
    assert set(r["selected_parameters"]) == {"threshold_pp"}
    assert r["selected_fill"] == "next_close"
    assert r["group"]["member_count"] == 1
    assert r["group"]["training_mean_pp"] > 0
    p = research_run(service, r["group"]["members"][0]["selection"], optimize=False)
    assert len(p["trades"]) > 0
    assert p["summary"]["ledger_reconciliation_rub"] == 0


def test_mixed_cohort_searches_common_mr_even_when_editor_is_funding_without_dropping(
    service,
):
    r = search(
        service,
        {**PAIR, "strategy": "funding_only", "parameters": {"lookback": 8}},
        targets=TARGETS,
        price_fill="next_close",
    )
    assert r["status"] == "maximum_found"
    assert r["selected_strategy"] == "mean_reversion"
    assert r["trial_count"] == TRIALS_PER_STRATEGY
    assert r["group"]["member_count"] == 2
    assert r["strategy_search"]["target_count"] == 2
    assert r["strategy_search"]["execution_policy"] == "native_rules"
    assert r["strategy_search"]["targets"] == [TARGETS[1], TARGETS[0]]
    assert "excluded" not in r
    assert {row["selection"]["fill"] for row in r["group"]["members"]} == {"next_close"}
    for record in r["candidates"]:
        if record["rank_score"] is not None:
            assert len(record["by_window_pp"]) == 2
            assert record["rank_score"] == pytest.approx(sum(record["by_window_pp"]) / 2)
    again = search(
        service,
        {**PAIR, "strategy": "funding_only", "parameters": {"lookback": 8}},
        targets=list(reversed(TARGETS)),
        price_fill="next_close",
    )
    assert r == again


def test_a_bad_strategy_is_skipped_not_its_window_when_another_covers_everyone(
    service, funded_root
):
    # No near perpetual EOD rows: no funding carry and no MR funding evidence.
    # Instead make only carry's far EOD cache unavailable; candle MR still works.
    for p in (funded_root / "data/carry-history-cache").glob("CRZ6-*.json"):
        p.unlink()
    r = search(service, targets=[TARGETS[0]])
    assert r["status"] == "maximum_found"
    assert r["selected_strategy"] == "mean_reversion"
    assert r["group"]["member_count"] == 1
    bad = next(a for a in r["strategy_search"]["alternatives"] if a["strategy"] == "funding_only")
    assert bad["status"] == "incompatible"
    assert bad["trial_count"] == 0
    assert bad["excluded"]
    assert r["trial_count"] == TRIALS_PER_STRATEGY


def test_universally_bad_members_need_confirmation_before_any_fit(service):
    req = {
        **PAIR,
        "targets": [
            *TARGETS,
            {"universe": "pair_screen", "pair": "unknown/unknown"},
        ],
    }
    with patch(
        "moex_spread_lab.proof.proof_strategy_search._tune",
        side_effect=AssertionError("no trials before confirmation"),
    ):
        r = search(service, req)
    assert r["status"] == "selection_requires_confirmation"
    assert r["remaining_count"] == 2
    assert len(r["excluded"]) == 1
    assert r["trial_count"] == 0
    assert r["selected_strategy"] is None


def test_different_strategy_failures_cannot_shrink_the_denominator(service):
    import moex_spread_lab.proof.proof_strategy_search as module

    # Deliberately disjoint per-family failures. Neither is universally bad.
    def prepare(service, request, settings, benchmark, _cancelled, **kwargs):
        target = (
            TARGETS[0]
            if request["strategy"] == "mean_reversion"
            else {**TARGETS[0], "pair": "CNYRUBF/CRM6"}
        )
        return {
            "excluded": [{"target": target, "reason": "synthetic incompatibility"}],
            "remaining_count": 1,
        }

    targets = [TARGETS[0], {**TARGETS[0], "pair": "CNYRUBF/CRM6"}]
    with (
        patch.object(module, "_prepare_cohort", side_effect=prepare),
        patch.object(
            module,
            "_tune",
            side_effect=AssertionError("never fit a smaller cohort"),
        ),
    ):
        r = search(service, targets=targets)
    assert r["status"] == "no_common_strategy"
    assert "excluded" not in r
    assert r["trial_count"] == 0
    assert r["selected_parameters"] is None


def test_all_pruned_candidates_leave_strategy_and_parameters_unchanged(service):
    def fail(*args):
        m = _measure(*args)
        return {
            **m,
            "rank_score": None,
            "utility": None,
            "reason": "synthetic unrankable",
        }

    req = {**PAIR, "strategy": "funding_only", "parameters": {"lookback": 8}}
    before = json.loads(json.dumps(req))
    with patch("moex_spread_lab.proof.proof_kernel._measure", side_effect=fail):
        r = search(service, req)
    assert r["status"] == "no_admissible_candidate"
    assert r["trial_count"] == 2 * TRIALS_PER_STRATEGY
    assert r["selected_parameters"] is None
    assert r["selected_strategy"] is None
    assert req == before
