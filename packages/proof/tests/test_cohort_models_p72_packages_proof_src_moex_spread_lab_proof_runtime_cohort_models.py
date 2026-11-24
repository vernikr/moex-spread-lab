"""Regression locks for the strict cohort-model helpers.

The cohort models replicate three small landscape/axis/cohort-preparation
contracts locally so this file can be typed without importing private symbols
from modules owned by other P7.2 leaves. These tests pin the replicas to the
reference implementations they mirror.
"""

import pytest

from moex_spread_lab.proof import cohort_models
from moex_spread_lab.proof.axis import Axis, _ordered
from moex_spread_lab.proof.cohort_prepare_helpers import _translate
from moex_spread_lab.proof.landscape import Landscape, _lamps


def _land(evaluate=lambda point: {"utility": 10.0, "rank_score": 1.0}):
    return Landscape({"lookback": Axis(3, 7, 1)}, evaluate)


@pytest.mark.parametrize(
    ("params", "strategy"),
    [
        ({}, "mean_reversion"),
        ({"window": 12, "entry_z": 1.5}, "mean_reversion"),
        ({"lookback": 9}, "zscore_mean_reversion"),
        ({"window": 4, "lookback": 8}, "funding_only"),
        ({"quantity": 2}, "convergence_to_ruonia"),
    ],
)
def test_translate_parameters_matches_preparation_translation(params, strategy):
    assert cohort_models._translate_parameters(dict(params), strategy) == _translate(
        dict(params), strategy
    )


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"exit_z": 1.5},
        {"entry_z": 1.5},
        {"exit_z": 2.0, "entry_z": 1.0},
        {"exit_z": 1.0, "entry_z": 2.0},
    ],
)
def test_ordered_values_matches_axis_predicate(values):
    assert cohort_models._ordered_values(dict(values)) == _ordered(dict(values))


def test_plateau_lamps_matches_reference_fields_and_cache():
    point = {"lookback": 5}
    candidate_land = _land()
    reference_land = _land()

    assert cohort_models._plateau_lamps(candidate_land, point, None) == _lamps(
        reference_land, point
    )
    assert candidate_land.cache == reference_land.cache


def test_plateau_lamps_matches_reference_reason_branch():
    point = {"lookback": 9}

    assert cohort_models._plateau_lamps(_land(), point, "причина") == _lamps(
        _land(), point, "причина"
    )


def test_plateau_lamps_matches_reference_probe_progress():
    point = {"lookback": 5}
    candidate_land = _land()
    reference_land = _land()
    candidate_events: list[tuple[int, int]] = []
    reference_events: list[tuple[int, int]] = []

    candidate = cohort_models._plateau_lamps(
        candidate_land,
        point,
        None,
        on_progress=lambda done, total: candidate_events.append((done, total)),
    )
    reference = _lamps(
        reference_land,
        point,
        on_progress=lambda done, total: reference_events.append((done, total)),
    )

    assert candidate == reference
    assert candidate_events == reference_events
    assert candidate_land.cache == reference_land.cache
