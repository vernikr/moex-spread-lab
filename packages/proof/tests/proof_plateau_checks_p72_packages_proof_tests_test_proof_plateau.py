"""Assertion helpers for the checked-plateau mean characterization (P7.2 health split).

Moved out of ``test_proof_plateau.py`` so that file's tests meet the code-health
complexity limits. The helpers only re-express the existing assertions over the
already measured plateau payload; they hold no scenario data, patch points or
numeric expectation.
"""

from __future__ import annotations

import unittest
from statistics import fmean
from typing import Any


def member_fields(solo_fields: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Per-member field for one axis, translating the profile's lookback alias."""
    return [solo_fields[0]["window" if name == "lookback" else name], solo_fields[1][name]]


def assert_center_utility_is_member_mean(
    case: unittest.TestCase, field: dict[str, Any], members: list[dict[str, Any]]
) -> float:
    center = fmean(member["center"]["rank_score"] for member in members)
    case.assertAlmostEqual(field["center"]["utility"], center)
    return center


def assert_neighbour_is_member_mean(
    case: unittest.TestCase, neighbour: dict[str, Any], members: list[dict[str, Any]]
) -> float | None:
    """Assert one neighbor is the member mean; return its utility mean when exposed."""
    readings = [
        next(x for x in member["neighbors"] if x["value"] == neighbour["value"])
        for member in members
    ]
    vals = [reading.get("rank_score") for reading in readings]
    if None in vals:
        case.assertIsNone(neighbour["utility"])
        case.assertIn("reason", neighbour)
        return None
    case.assertEqual(neighbour["by_window_pp"], vals)
    average = fmean(vals)
    case.assertAlmostEqual(neighbour["rank_score"], average)
    if any(reading["utility"] is not None for reading in readings):
        case.assertAlmostEqual(neighbour["utility"], average)
        return average
    return None


def assert_neighbours_are_member_means(
    case: unittest.TestCase, field: dict[str, Any], members: list[dict[str, Any]]
) -> list[float]:
    expected = []
    for neighbour in field["neighbors"]:
        average = assert_neighbour_is_member_mean(case, neighbour, members)
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
