"""Issue #85 regressions: strict-typed landscape keeps its runtime and facade contracts."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof import axis as axis_module
from moex_spread_lab.proof import landscape, proof_kernel, proof_search
from moex_spread_lab.proof.axis import Axis


def _axes() -> dict[str, Axis]:
    return {"window": Axis(0.5, 2.0, 0.1)}


def _center() -> dict[str, float]:
    return {"window": 1.0}


def test_private_facade_bindings_keep_object_identity() -> None:
    assert landscape._ordered is axis_module._ordered
    assert proof_kernel._lamps is proof_search._lamps is landscape._lamps
    assert proof_kernel._ordered is proof_search._ordered
    assert proof_kernel.Landscape is proof_search.Landscape is landscape.Landscape
    assert (landscape.GREEN, landscape.AMBER) == (70, 40)


def test_measure_caches_by_axis_tuple_and_records_w0_failure() -> None:
    calls: list[dict[str, float]] = []

    def evaluate(point: Mapping[str, float]) -> dict[str, Any]:
        calls.append(dict(point))
        if point["window"] == 1.2:
            raise W0Error("unavailable")
        return {"utility": float(point["window"]), "rank_score": 1}

    land = landscape.Landscape(_axes(), evaluate)
    first = land.measure(_center())
    assert first == {"utility": 1.0, "rank_score": 1}
    assert land.measure(_center()) is first
    failed = land.measure({"window": 1.2})
    assert failed == {"utility": None, "reason": "unavailable"}
    assert land.measure({"window": 1.2}) is failed
    assert len(calls) == 2
    assert set(land.cache) == {(1.0,), (1.2,)}


def test_profile_plateau_ratio_and_neighbor_payloads() -> None:
    land = landscape.Landscape(_axes(), lambda _: {"utility": 10.0})
    result = land.profile(_center(), "window")
    assert result["value"] == 1.0
    assert result["score"] == 100.0
    assert result["worst"] == 10.0
    assert result["reason"] is None
    assert result["step"] == 0.1
    assert result["center"] == {"utility": 10.0}
    assert [row["value"] for row in result["neighbors"]] == [0.8, 0.9, 1.1, 1.2]
    assert all(row["utility"] == 10.0 for row in result["neighbors"])


def test_profile_nonpositive_center_and_invalid_neighbor() -> None:
    losing = landscape.Landscape(_axes(), lambda _: {"utility": -2.0})
    negative = losing.profile(_center(), "window")
    assert negative["score"] == 0.0
    assert negative["reason"] == "Результат текущего значения на обучении неположителен."

    def evaluate(point: Mapping[str, float]) -> dict[str, Any]:
        if point["window"] == 0.9:
            return {"utility": None, "reason": "missing"}
        return {"utility": 10.0}

    partial = landscape.Landscape(_axes(), evaluate)
    blocked = partial.profile(_center(), "window")
    assert blocked["score"] == 0.0
    assert blocked["reason"] == "Есть сосед без допустимого результата."


def test_profile_without_valid_neighbors_keeps_null_score() -> None:
    land = landscape.Landscape(_axes(), lambda _: {"utility": None})
    result = land.profile(_center(), "window")
    assert result["score"] is None
    assert result["worst"] is None
    assert result["reason"] == "Нет допустимых соседних значений для сравнения."
    assert len(result["neighbors"]) == 4


def test_probe_points_deduplicate_and_skip_cached_entries() -> None:
    land = landscape.Landscape(_axes(), lambda _: {"utility": 1.0})
    point = _center()
    probes = landscape._probe_points(land, point)
    assert [probe["window"] for probe in probes] == [1.0, 0.8, 0.9, 1.1, 1.2]
    assert probes[0] is point
    land.measure(point)
    land.measure({"window": 0.9})
    assert [probe["window"] for probe in landscape._probe_points(land, point)] == [
        0.8,
        1.1,
        1.2,
    ]


def test_lamps_progress_counts_only_uncached_probes() -> None:
    calls: list[dict[str, float]] = []
    events: list[tuple[int, int]] = []

    def evaluate(point: Mapping[str, float]) -> dict[str, Any]:
        calls.append(dict(point))
        return {"utility": 10.0}

    land = landscape.Landscape(_axes(), evaluate)
    land.measure(_center())
    result = landscape._lamps(
        land, _center(), on_progress=lambda done, total: events.append((done, total))
    )
    assert events == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert len(calls) == 5
    assert sorted(result) == ["amber", "basis", "fields", "green", "unit"]
    assert result["basis"] == "training"
    assert result["unit"] == "annualized_excess_pp"
    assert (result["green"], result["amber"]) == (70, 40)
    assert list(result["fields"]) == ["window"]
    assert result == landscape._lamps(land, _center())
    assert len(calls) == 5


def test_lamps_reason_short_circuits_measurement_and_progress() -> None:
    def evaluate(_: Mapping[str, float]) -> dict[str, Any]:
        raise AssertionError("reason short-circuit must not measure")

    land = landscape.Landscape(_axes(), evaluate)
    events: list[tuple[int, int]] = []
    result = landscape._lamps(
        land,
        _center(),
        reason="blocked",
        on_progress=lambda done, total: events.append((done, total)),
    )
    assert events == []
    assert result["fields"]["window"] == {
        "value": 1.0,
        "score": None,
        "reason": "blocked",
        "neighbors": [],
    }
    assert land.cache == {}


def test_module_level_ordered_hook_stays_patchable(monkeypatch: pytest.MonkeyPatch) -> None:
    land = landscape.Landscape(_axes(), lambda _: {"utility": 1.0})
    monkeypatch.setattr(landscape, "_ordered", lambda values: False)
    assert landscape._probe_points(land, _center()) == [_center()]
    assert land.profile(_center(), "window")["neighbors"] == []


def test_duck_typed_axes_and_none_evaluator_are_accepted() -> None:
    axes = {"window": SimpleNamespace(step=1, contains=lambda value: True)}
    land = landscape.Landscape(axes, None)
    for value in (0, 1, 2, 3, 4):
        land.cache[(value,)] = {"rank_score": 1.0, "utility": 1.0}
    assert land.measure({"window": 2}) == {"rank_score": 1.0, "utility": 1.0}
    result = landscape._lamps(land, {"window": 2})
    assert result["fields"]["window"]["score"] == 100.0
