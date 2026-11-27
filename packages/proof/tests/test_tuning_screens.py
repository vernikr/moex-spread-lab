"""Tuning exceptions waive two named screens only; they do not fund larger books."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from moex_spread_lab.core.settings import (
    ResearchSettings,
    read_settings,
    save_settings,
    settings_path,
)
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.proof_batch import checked_plateau
from moex_spread_lab.proof.proof_search import TRIALS, _tune, research_run
from moex_spread_lab.proof.service import ProofService
from proof_fixtures import screen_failures
from test_proof_batch import ACTIVE, C, P


def _assert_excluded_members(
    preview: dict,
    result: dict,
    excluded: set[str],
    svc: ProofService,
    req: dict,
    thin: dict,
    allow_volume: bool,
    capital: dict,
) -> None:
    assert result["status"] == "selection_requires_confirmation"
    assert result["trial_count"] == 0
    assert result["remaining_count"] == 2 - len(excluded)
    assert preview["plateau"] is None
    assert preview["excluded"] == result["excluded"]
    assert {x["target"]["pair"] for x in result["excluded"]} == excluded
    # A rejected member does not imply the other, waived (but failing) gate is enforced.
    if len(excluded) == 1:
        remaining = thin if allow_volume else capital
        single = research_run(svc, {**req, "targets": [remaining]}, optimize=True)
        assert single["status"] == "maximum_found"


def _assert_selected_book(
    result: dict,
    share: float,
    svc: ProofService,
    req: dict,
    thin: dict,
) -> None:
    assert result["status"] == "maximum_found"
    assert result["trial_count"] == TRIALS
    assert result["group"]["member_count"] == 2
    assert result["group"]["result_mean_pp"] is not None
    assert (
        checked_plateau(svc, {**req, "parameters": result["selected_parameters"]})["plateau"]
        == result["plateau"]
    )
    for member in result["group"]["members"]:
        _assert_member_lamps(member, thin, share, svc)
    # Original stricter raw proof consumer is unaffected by the research settings.
    with pytest.raises(W0Error):
        svc.proof(ACTIVE)


def _assert_member_lamps(
    member: dict,
    thin: dict,
    share: float,
    svc: ProofService,
) -> None:
    expected = "volume" if member["target"] == thin else "capital"
    evaluation = member["evaluation"]
    assert evaluation["failed_screens"] == [expected]
    assert evaluation["validation_percent"] == share
    rerun = research_run(svc, member["selection"], optimize=False)
    assert rerun["evaluation"] == evaluation
    assert rerun["run_id"] == member["run_id"]
    window = rerun["window"]
    failed = (
        window["tradable_every_day"] if expected == "volume" else window["passes_capital_screen"]
    )
    assert not failed


def test_migration_partial_writes_and_last_snapshot(tmp_path: Path):
    root = tmp_path
    path = settings_path(root)
    path.parent.mkdir(parents=True)
    legacy = '{"validation_percent": 17.5}\n'
    path.write_text(legacy)
    assert read_settings(root) == {
        "validation_percent": 17.5,
        "allow_failed_volume": True,
        "allow_failed_capital": True,
        "slippage_ticks": 1.0,
        "initial_capital_rub": 30000.0,
        "timeframe": "1d",
        "price_fill": "reference_same_bar",
        "random_seed": 0,
    }
    assert path.read_text() == legacy
    save_settings(root, {"allow_failed_volume": False})
    save_settings(root, {"validation_percent": 31.5})
    latest = save_settings(root, {"allow_failed_capital": False})
    assert latest == {
        "allow_failed_volume": False,
        "allow_failed_capital": False,
        "validation_percent": 31.5,
        "slippage_ticks": 1.0,
        "initial_capital_rub": 30000.0,
        "timeframe": "1d",
        "price_fill": "reference_same_bar",
        "random_seed": 0,
    }


@pytest.mark.parametrize(
    "key",
    ["allow_failed_volume", "allow_failed_capital"],
    ids=["volume", "capital"],
)
@pytest.mark.parametrize(
    "bad",
    [None, "false", 1, 0, [], {}],
    ids=["none", "str", "one", "zero", "list", "dict"],
)
def test_boolean_settings_rejected_without_partial_writes(tmp_path: Path, key, bad):
    root = tmp_path
    save_settings(root, {"allow_failed_volume": False})
    save_settings(root, {"validation_percent": 31.5})
    latest = save_settings(root, {"allow_failed_capital": False})
    with pytest.raises(W0Error):
        save_settings(root, {key: bad, "validation_percent": 0})
    assert read_settings(root) == latest


@pytest.mark.parametrize(
    ("allow_volume", "allow_capital"),
    [(True, True), (False, True), (True, False), (False, False)],
    ids=["both-allowed", "volume-denied", "capital-denied", "both-denied"],
)
@pytest.mark.parametrize(
    ("thin_calendar", "share"),
    [(False, 0), (True, 25)],
    ids=["wide", "thin"],
)
def test_all_four_combinations_in_preflight_search_report_and_live_lamps(
    tmp_path_factory: pytest.TempPathFactory,
    thin_calendar,
    share,
    allow_volume,
    allow_capital,
):
    root = tmp_path_factory.mktemp("screen-waivers")
    screen_failures(root, thin_calendar=thin_calendar)
    svc = ProofService(root)
    req = {**ACTIVE, "targets": [P, C]}
    thin, capital = (C, P) if thin_calendar else (P, C)
    cfg = save_settings(
        root,
        {
            "validation_percent": share,
            "allow_failed_volume": allow_volume,
            "allow_failed_capital": allow_capital,
        },
    )
    excluded = {
        target["pair"]
        for target, allowed in ((thin, allow_volume), (capital, allow_capital))
        if not allowed
    }
    preview = checked_plateau(svc, req)
    result = research_run(svc, req, optimize=True)
    assert preview["member_count"] == 2
    for key, value in cfg.items():
        assert preview[key] == value
    if excluded:
        _assert_excluded_members(preview, result, excluded, svc, req, thin, allow_volume, capital)
    else:
        _assert_selected_book(result, share, svc, req, thin)


def test_mid_search_settings_change_only_affects_next_request(tmp_path_factory):
    root = tmp_path_factory.mktemp("mid-search")
    screen_failures(root)
    svc = ProofService(root)
    req = {**ACTIVE, "targets": [P, C]}

    def change_while_running(*args, **kwargs):
        save_settings(
            root,
            {
                "allow_failed_volume": False,
                "allow_failed_capital": False,
                "validation_percent": 50,
            },
        )
        return _tune(*args, **kwargs)

    with patch("moex_spread_lab.proof.proof_batch._tune", side_effect=change_while_running):
        r = research_run(svc, req, optimize=True)
    for e in [r["group"], *[m["evaluation"] for m in r["group"]["members"]]]:
        for k, v in ResearchSettings().metadata().items():
            assert e[k] == v
    assert r["group"]["result_mean_pp"] is not None
    next_ = research_run(svc, req, optimize=True)
    assert next_["remaining_count"] == 0
    assert next_["validation_percent"] == 50


def test_no_free_margin_new_data_or_risk_override(tmp_path_factory):
    root = tmp_path_factory.mktemp("free-margin")
    screen_failures(root, thin_calendar=True)
    svc = ProofService(root)
    # ~36k posted margin cannot be financed from the independent 30k account.
    req = {
        **ACTIVE,
        "quantity": 3,
        "parameters": {"lookback": 6, "entry_z": 0.8, "exit_z": 0.1},
    }
    r = research_run(svc, {**req, "targets": [P, C]}, optimize=True)
    assert [x["target"] for x in r["excluded"]] == [P]
    assert "превышает стартовый капитал" in r["excluded"][0]["reason"]
    # The calendar engine can replay an observed entry, but will not rank a negative-cash book.
    cal = {
        **C,
        "strategy": "zscore_mean_reversion",
        "quantity": 6,
        "parameters": {"window": 10, "entry_z": 1.5, "exit_z": 0.5},
    }
    proof = research_run(svc, cal, optimize=False)
    assert len(proof["trades"]) > 0
    assert not proof["window"]["passes_capital_screen"]
    assert proof["trades"][0]["margin_rub"] > 30000
    r = proof["evaluation"]["result"]
    assert "недостаточны для маржи" in r["reason"]
    assert r.get("annualized_excess_pp") is None
    assert all(f["score"] is None for f in proof["plateau"]["fields"].values())
    # Missing funding data isn't a volume exception. HTTP flags in a proof request are not an override.
    next((root / "data/carry-history-cache").glob("CNYRUBF-*.json")).unlink()
    r = research_run(svc, {**ACTIVE, "targets": [P, C]}, optimize=True)
    assert [x["target"] for x in r["excluded"]] == [P]
    assert "локальных данных" in r["excluded"][0]["reason"]
    assert r["trial_count"] == 0
    save_settings(root, {"allow_failed_volume": False})
    r = checked_plateau(svc, {**ACTIVE, "targets": [C], "allow_failed_volume": True})
    assert r["plateau"] is None
    assert not r["allow_failed_volume"]


def test_two_failed_screens_on_one_carry_window_still_allow_the_search(tmp_path_factory):
    root = tmp_path_factory.mktemp("two-failures")
    screen_failures(root, thin_calendar=True)
    svc = ProofService(root)
    path = next((root / "data/carry-history-cache").glob("CRZ6-*.json"))
    doc = json.loads(path.read_text())
    h = doc["payload"]["history"]
    v = h["columns"].index("VOLUME")
    h["data"][4][v] = 3
    path.write_text(json.dumps(doc))
    req = {
        **P,
        "strategy": "funding_only",
        "parameters": {"lookback": 8},
        "targets": [P],
    }
    r = research_run(svc, req, optimize=True)
    assert r["status"] == "maximum_found"
    assert r["evaluation"]["failed_screens"] == ["volume", "capital"]


@pytest.mark.parametrize(
    ("volume", "capital"),
    [(False, True), (True, False), (False, False)],
    ids=["volume-denied", "capital-denied", "both-denied"],
)
def test_waiving_the_failed_screens_leaves_nothing_to_search(tmp_path_factory, volume, capital):
    root = tmp_path_factory.mktemp("waive-both")
    screen_failures(root, thin_calendar=True)
    svc = ProofService(root)
    path = next((root / "data/carry-history-cache").glob("CRZ6-*.json"))
    doc = json.loads(path.read_text())
    h = doc["payload"]["history"]
    v = h["columns"].index("VOLUME")
    h["data"][4][v] = 3
    path.write_text(json.dumps(doc))
    req = {
        **P,
        "strategy": "funding_only",
        "parameters": {"lookback": 8},
        "targets": [P],
    }
    save_settings(root, {"allow_failed_capital": capital, "allow_failed_volume": volume})
    r = research_run(svc, req, optimize=True)
    assert r["remaining_count"] == 0
    assert r["trial_count"] == 0


def test_no_volume_observations_leave_the_plateau_unavailable(tmp_path_factory):
    root = tmp_path_factory.mktemp("no-observations")
    screen_failures(root, thin_calendar=True)
    svc = ProofService(root)
    path = next((root / "data/carry-history-cache").glob("CRZ6-*.json"))
    doc = json.loads(path.read_text())
    h = doc["payload"]["history"]
    v = h["columns"].index("VOLUME")
    save_settings(root, ResearchSettings().metadata())
    for row in h["data"]:
        row[v] = None
    path.write_text(json.dumps(doc))
    req = {
        **P,
        "strategy": "funding_only",
        "parameters": {"lookback": 8},
        "targets": [P],
    }
    r = checked_plateau(svc, req)
    assert r["plateau"] is None
    assert "Нет наблюдений" in r["excluded"][0]["reason"]


def test_missing_reference_margin_is_not_a_stress_failure_with_known_go(tmp_path_factory):
    root = tmp_path_factory.mktemp("missing-margin")
    screen_failures(root, thin_calendar=True)
    svc = ProofService(root)
    # A missing reference margin is not a stress failure with known, financeable GO.
    path = root / "data/w1/current-intake.json"
    doc = json.loads(path.read_text())
    next(i for i in doc["instruments"] if i["secid"] == "SRZ6")["initial_margin"] = None
    path.write_text(json.dumps(doc))
    r = checked_plateau(svc, {**ACTIVE, "targets": [C]})
    assert r["plateau"] is None
    assert r["excluded"][0]["target"] == C
