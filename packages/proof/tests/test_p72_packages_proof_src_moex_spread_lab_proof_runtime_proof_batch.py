"""Isolated P7.2 regression tests for proof_runtime/proof_batch.py."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from moex_spread_lab.core import w0
from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof import (
    cohort_models,
    cohort_prepare_helpers,
    cohort_reporting,
    proof_batch,
    proof_kernel,
    proof_pool,
    remote_cohort_types,
    request_validation,
    service,
)
from moex_spread_lab.proof.cohort_prepare import PreparationInputs
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData


@dataclass
class _FakeService:
    root: Path
    _calendar: Any = None


def test_facade_reexports_preserve_exact_object_identity():
    pairs = [
        ("_run_id", w0),
        ("_Cohort", cohort_models),
        ("_RemoteCohort", cohort_models),
        ("MAX_TARGETS", cohort_prepare_helpers),
        ("_active_seed", cohort_prepare_helpers),
        ("_confirmation", cohort_prepare_helpers),
        ("_filter_training", cohort_prepare_helpers),
        ("_intersect_axes", cohort_prepare_helpers),
        ("_native_request", cohort_prepare_helpers),
        ("_targets", cohort_prepare_helpers),
        ("_translate", cohort_prepare_helpers),
        ("_unavailable", cohort_reporting),
        ("TRIALS", proof_kernel),
        ("StudyWindow", proof_kernel),
        ("Axis", proof_kernel),
        ("_tune", proof_kernel),
        ("_validate", proof_kernel),
        ("_lamps", proof_kernel),
        ("_ordered", proof_kernel),
        ("_estimate_weight", proof_pool),
        ("get_worker_pool", proof_pool),
        ("_RemoteCase", remote_cohort_types),
        ("_RemoteSplit", remote_cohort_types),
        ("_TrainingSlice", remote_cohort_types),
        ("_identity", request_validation),
        ("_benchmark_marks", service),
    ]
    for name, owner in pairs:
        assert getattr(proof_batch, name) is getattr(owner, name), name


def test_call_with_optional_progress_inspects_target_signature():
    cb = object()

    def explicit(x: int, *, on_progress: object = None) -> tuple[int, object]:
        return x, on_progress

    def var_kw(x: int, **kwargs: object) -> tuple[int, dict[str, object]]:
        return x, kwargs

    def plain(x: int) -> int:
        return x * 2

    assert proof_batch._call_with_optional_progress(explicit, 3, on_progress=cb) == (3, cb)
    assert proof_batch._call_with_optional_progress(var_kw, 3, on_progress=cb) == (
        3,
        {"on_progress": cb},
    )
    assert proof_batch._call_with_optional_progress(plain, 3, on_progress=cb) == 6
    assert proof_batch._call_with_optional_progress(explicit, 3, on_progress=None) == (3, None)
    with patch.object(proof_batch.inspect, "signature", side_effect=ValueError("uninspectable")):
        assert proof_batch._call_with_optional_progress(plain, 4, on_progress=cb) == 8


def test_prepare_cohort_and_cohort_result_reject_unexpected_kwargs_and_forward_options():
    captured: list[PreparationInputs] = []
    settings = ResearchSettings(price_fill="reference_same_bar")
    sentinel_service = _FakeService(root=Path("/tmp"))
    sentinel_cal = object()
    sentinel_reg = object()
    progress_cb = Mock()

    def fake_prepare(inputs: PreparationInputs) -> dict[str, str]:
        captured.append(inputs)
        return {"status": "ok"}

    with patch.object(proof_batch, "prepare_cohort", side_effect=fake_prepare):
        out = proof_batch._prepare_cohort(
            sentinel_service,
            {"strategy": "mean_reversion"},
            settings,
            {},
            lambda: None,
            calendar=sentinel_cal,
            on_progress=progress_cb,
            registry=sentinel_reg,
        )
    assert out == {"status": "ok"}
    inp = captured[0]
    assert inp.calendar is sentinel_cal
    assert inp.on_progress is progress_cb
    assert inp.registry is sentinel_reg
    assert inp.window_factory is proof_batch.StudyWindow
    assert inp.can_use_pool is proof_batch._can_use_pool

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        proof_batch._prepare_cohort(
            sentinel_service,
            {},
            settings,
            {},
            lambda: None,
            bogus=1,
        )

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        proof_batch._cohort_result({}, settings, object(), None, 0, [], lambda: None, extra=True)


def test_can_use_pool_guards_study_window_and_exact_calendar_type():
    class _SubCalendar(CalendarData):
        pass

    svc = _FakeService(root=Path("/tmp"))
    specs = [(0, {"universe": "pair_screen", "pair": "A/B"}, {})]
    cal = object.__new__(CalendarData)
    sub_cal = object.__new__(_SubCalendar)
    pool = Mock()
    pool.should_use.return_value = True

    with (
        patch.object(proof_batch, "get_worker_pool", return_value=pool),
        patch.object(proof_batch, "_estimate_weight", return_value=500),
    ):
        assert proof_batch._can_use_pool(svc, cal, specs, "1d") is True
        pool.should_use.assert_called_once_with(1, 500)
        assert proof_batch._can_use_pool(svc, sub_cal, specs, "1d") is False
        with patch.object(proof_batch, "StudyWindow", object()):
            assert proof_batch._can_use_pool(svc, cal, specs, "1d") is False


def test_checked_plateau_validates_emits_progress_and_closes_group():
    svc = _FakeService(root=Path("/tmp"))
    settings = ResearchSettings(validation_percent=25, price_fill="reference_same_bar")
    target = {"universe": "pair_screen", "pair": "A/B"}
    base_req = {
        "targets": [target],
        "strategy": "mean_reversion",
        "parameters": {"lookback": 10, "entry_z": 1.5, "exit_z": 0.5},
        "extra_ignored": "drop_me",
    }

    with (
        patch.object(proof_batch, "_validate"),
        pytest.raises(W0Error, match="отмеченные окна"),
    ):
        proof_batch.checked_plateau(svc, {"strategy": "mean_reversion"})

    with (
        patch.object(proof_batch, "_validate"),
        patch.object(proof_batch, "_targets", return_value=[target]),
        patch.object(proof_batch, "get_strategy_axes", return_value={"mean_reversion": {"a": 1}}),
        pytest.raises(W0Error, match="общие параметры"),
    ):
        proof_batch.checked_plateau(svc, base_req)

    events: list[dict[str, Any]] = []
    closed = Mock()

    def fake_prepare(*_args: object, on_progress: Any = None, **_kwargs: object) -> object:
        if on_progress is not None:
            on_progress(1, 1)
        return SimpleNamespace(
            seed={"lookback": 10},
            plateau=lambda _seed, *, on_progress=None: (
                on_progress(1, 2) if on_progress else None,
                {"green": 80},
            )[1],
            close=closed,
        )

    with (
        patch.object(proof_batch, "_validate"),
        patch.object(proof_batch, "_targets", return_value=[target]),
        patch.object(
            proof_batch,
            "get_strategy_axes",
            return_value={"mean_reversion": {"lookback": 1, "entry_z": 1, "exit_z": 1}},
        ),
        patch.object(proof_batch, "_benchmark_marks", return_value={}),
        patch.object(proof_batch, "_prepare_cohort", side_effect=fake_prepare),
    ):
        measured = proof_batch.checked_plateau(
            svc,
            base_req,
            settings_override=settings,
            on_progress=events.append,
        )
    assert measured["status"] == "measured"
    assert measured["scope"] == "checked"
    assert measured["basis"] == "training"
    assert measured["execution_reference"] is True
    assert measured["plateau"] == {"green": 80}
    closed.assert_called_once_with()
    assert [e["stage"] for e in events] == ["prepare", "prepare", "plateau", "plateau"]
    assert [e["percent"] for e in events] == [5.0, 45.0, 45.0, 71.5]

    unavailable_group = {"excluded": [{"target": target, "reason": "short"}]}
    with (
        patch.object(proof_batch, "_validate"),
        patch.object(proof_batch, "_targets", return_value=[target]),
        patch.object(
            proof_batch,
            "get_strategy_axes",
            return_value={"mean_reversion": {"lookback": 1, "entry_z": 1, "exit_z": 1}},
        ),
        patch.object(proof_batch, "_benchmark_marks", return_value={}),
        patch.object(proof_batch, "_prepare_cohort", return_value=unavailable_group),
    ):
        unavail = proof_batch.checked_plateau(
            svc,
            base_req,
            settings_override=settings,
        )
    assert unavail["status"] == "unavailable"
    assert unavail["excluded"] == unavailable_group["excluded"]
    assert unavail["plateau"] is None


def test_batch_search_handles_unavailable_search_progress_and_close_on_error():
    svc = _FakeService(root=Path("/tmp"))
    settings = ResearchSettings(random_seed=41)
    req = {"targets": [{"universe": "pair_screen", "pair": "A/B"}], "strategy": "mean_reversion"}

    with (
        patch.object(proof_batch, "_targets", return_value=req["targets"]),
        patch.object(proof_batch, "_prepare_cohort", return_value={"status": "unavailable"}),
    ):
        assert proof_batch.batch_search(svc, req, settings, {}, lambda: None) == {
            "status": "unavailable"
        }

    events: list[dict[str, Any]] = []
    closed = Mock()
    members = [
        (req["targets"][0], SimpleNamespace(split=SimpleNamespace(training=[1, 2, 3, 4]))),
        ({"pair": "C/D"}, SimpleNamespace(split=SimpleNamespace(training=[1, 2]))),
    ]
    group = SimpleNamespace(
        axes={"lookback": object()},
        seed={"lookback": 10},
        members=members,
        land=object(),
        close=closed,
    )

    def fake_prepare(*_args: object, on_progress: Any = None, **_kwargs: object) -> object:
        if on_progress is not None:
            on_progress(1, 1)
        return group

    def fake_tune(
        _land: object,
        _seed: object,
        _axes: object,
        min_train: int,
        *,
        random_seed: int,
        seed_label: str,
        on_progress: Any = None,
    ) -> tuple[dict[str, int], int, list[dict[str, int]]]:
        assert min_train == 2
        assert random_seed == 41
        assert seed_label == "cohort"
        if on_progress is not None:
            on_progress(10, 0)
        return {"lookback": 10}, 10, [{"lookback": 10}]

    with (
        patch.object(proof_batch, "_targets", return_value=req["targets"]),
        patch.object(proof_batch, "_prepare_cohort", side_effect=fake_prepare),
        patch.object(proof_batch, "_tune", side_effect=fake_tune),
        patch.object(proof_batch, "_cohort_result", return_value={"status": "maximum_found"}),
    ):
        res = proof_batch.batch_search(
            svc,
            req,
            settings,
            {},
            lambda: None,
            on_progress=events.append,
        )
    assert res == {"status": "maximum_found"}
    closed.assert_called_once_with()
    assert events[-1] == {
        "stage": "search",
        "done": 10,
        "total": proof_batch.TRIALS,
        "percent": round(12.0 + 76.0 * (10 / proof_batch.TRIALS), 1),
        "label": "Optuna: испытание 10/0",
    }

    closed_on_err = Mock()
    err_group = SimpleNamespace(
        axes={}, seed={}, members=members, land=object(), close=closed_on_err
    )
    with (
        patch.object(proof_batch, "_targets", return_value=req["targets"]),
        patch.object(proof_batch, "_prepare_cohort", return_value=err_group),
        patch.object(proof_batch, "_tune", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        proof_batch.batch_search(svc, req, settings, {}, lambda: None)
    closed_on_err.assert_called_once_with()
