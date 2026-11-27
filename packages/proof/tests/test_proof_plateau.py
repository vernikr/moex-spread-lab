"""The live checked-window diagnostics must profile the mean, not a selected row or mean of colors."""

import json
from collections.abc import Iterator
from statistics import fmean
from typing import Any
from unittest.mock import patch

import pytest

from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.proof_batch import checked_plateau
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.service import ProofService
from proof_fixtures import fixture
from test_proof_batch import ACTIVE, C, P

REQ = {
    "strategy": ACTIVE["strategy"],
    "parameters": ACTIVE["parameters"],
    "targets": [C, P],
}


@pytest.fixture
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ProofService]:
    root = tmp_path_factory.mktemp("plateau-root")
    fixture(root)
    save_settings(root, {"validation_percent": 25})
    yield ProofService(root)


def profile(service: ProofService, **kw):
    return checked_plateau(service, {**REQ, **kw})


def member_fields(solo_fields: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Per-member field for one axis, translating the profile's lookback alias."""
    return [solo_fields[0]["window" if name == "lookback" else name], solo_fields[1][name]]


def assert_center_utility_is_member_mean(
    field: dict[str, Any], members: list[dict[str, Any]]
) -> float:
    center = fmean(member["center"]["rank_score"] for member in members)
    assert field["center"]["utility"] == pytest.approx(center)
    return center


def assert_neighbours_are_member_means(
    field: dict[str, Any], members: list[dict[str, Any]]
) -> list[float]:
    expected = []
    for neighbour in field["neighbors"]:
        average = assert_neighbour_is_member_mean(neighbour, members)
        if average is not None:
            expected.append(average)
    return expected


def expected_stability_ratio(
    field: dict[str, Any], expected: list[float], center: float
) -> float | None:
    if not expected:
        return None
    if len(expected) != len(field["neighbors"]) or center <= 0:
        return 0
    return round(100 * max(0, min(1, min(expected) / center)), 2)


def lamp_differs_from_member_average(members: list[dict[str, Any]], score: float | None) -> bool:
    if score is None or not all(member["score"] is not None for member in members):
        return False
    return abs(score - fmean(member["score"] for member in members)) > 0.1


def assert_neighbour_is_member_mean(
    neighbour: dict[str, Any], members: list[dict[str, Any]]
) -> float | None:
    """Assert one neighbor is the member mean; return its utility mean when exposed."""
    readings = [
        next(x for x in member["neighbors"] if x["value"] == neighbour["value"])
        for member in members
    ]
    vals = [reading.get("rank_score") for reading in readings]
    if None in vals:
        assert neighbour["utility"] is None
        assert "reason" in neighbour
        return None
    assert neighbour["by_window_pp"] == vals
    average = fmean(vals)
    assert neighbour["rank_score"] == pytest.approx(average)
    if any(reading["utility"] is not None for reading in readings):
        assert neighbour["utility"] == pytest.approx(average)
        return average
    return None


def test_mean_at_center_and_every_neighbour_precedes_the_stability_ratio(service):
    reqs = [
        {
            **C,
            "strategy": "zscore_mean_reversion",
            "parameters": {"window": 10, "entry_z": 1.5, "exit_z": 0.5},
        },
        ACTIVE,
    ]
    solo = [research_run(service, r, optimize=False)["plateau"]["fields"] for r in reqs]
    # Probes must not start a study, evaluate the holdout or write a trial log.
    with (
        patch(
            "moex_spread_lab.proof.proof_batch._tune",
            side_effect=AssertionError("not a search"),
        ),
        patch(
            "moex_spread_lab.proof.proof_search.StudyWindow.report",
            side_effect=AssertionError("not a validation report"),
        ),
    ):
        all_ = profile(service)
    assert (all_["status"], all_["member_count"], all_["validation_percent"]) == (
        "measured",
        2,
        25,
    )
    p = all_["plateau"]
    assert p["basis"] == "training"
    differs = False
    for name, field in p["fields"].items():
        fs = member_fields(solo, name)
        center = assert_center_utility_is_member_mean(field, fs)
        expected = assert_neighbours_are_member_means(field, fs)
        score = expected_stability_ratio(field, expected, center)
        assert field["score"] == score
        differs |= lamp_differs_from_member_average(fs, score)
    assert differs, "fixture must detect an average-of-individual-lamps implementation"
    assert not (service.root / "data/analytics/proof-searches.jsonl").exists()


def test_no_focus_order_or_holdout_leak_and_exact_winner_profile(service):
    save_settings(service.root, {"allow_failed_volume": False})
    before = profile(service)
    assert before == profile(service, **P)
    assert before == profile(service, **C, targets=[P, C])
    opt = research_run(service, REQ, optimize=True)
    assert profile(service, parameters=opt["selected_parameters"])["plateau"] == opt["plateau"]
    path = service.root / "data/w1/candles/CRZ6_1d.json"
    data = json.loads(path.read_text())
    data["rows"][-2]["volume"] = 1
    data["rows"][-2]["close"] += 3
    path.write_text(json.dumps(data))
    assert profile(service) == before
    alone = profile(service, targets=[P], **C)
    assert alone["member_count"] == 1
    assert alone["plateau"] == research_run(service, ACTIVE, optimize=False)["plateau"]
    save_settings(service.root, {"validation_percent": 0})
    blocked = profile(service)
    assert blocked["status"] == "unavailable"
    assert blocked["plateau"] is None
    assert blocked["excluded"][0]["target"] == P


def test_incompatible_or_invalid_center_is_not_profiled_on_a_subset_or_clamped(service):
    bad = profile(service, strategy="funding_only", parameters={"lookback": 10})
    assert bad["member_count"] == 2
    assert bad["plateau"] is None
    assert [x["target"] for x in bad["excluded"]] == [C]
    assert "selected_parameters" not in bad
    assert (
        profile(service, strategy="funding_only", parameters={"lookback": 10}, targets=[C])[
            "plateau"
        ]
        is None
    )
    wide = profile(service, parameters={**ACTIVE["parameters"], "lookback": 70})
    fields = wide["plateau"]["fields"]
    assert fields["lookback"]["value"] == 70
    assert all(f["score"] is None and "диапазон" in f["reason"] for f in fields.values())


@pytest.mark.parametrize(
    "bad_req",
    [
        {"targets": []},
        {"targets": [C, C]},
        {"parameters": {"entry_z": 1.5}},
        {"parameters": {**ACTIVE["parameters"], "exit_z": 3}},
    ],
    ids=["no-targets", "duplicate", "no-exit", "exit-above-entry"],
)
def test_invalid_center_requests_fail_closed(service, bad_req):
    with pytest.raises(W0Error):
        profile(service, **bad_req)


def test_checked_plateau_observes_cancellation(service):
    with pytest.raises(ConnectionAbortedError):
        checked_plateau(
            service,
            REQ,
            check_cancelled=lambda: (_ for _ in ()).throw(ConnectionAbortedError()),
        )


def test_checked_plateau_is_read_only_and_repeatable(service):
    result = checked_plateau(service, REQ)
    assert result == profile(service)
    assert not (service.root / "data/analytics/proof-searches.jsonl").exists()
