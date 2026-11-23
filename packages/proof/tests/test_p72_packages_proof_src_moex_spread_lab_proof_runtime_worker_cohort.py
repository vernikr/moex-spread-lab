"""Isolated regression coverage for the strict-typed worker cohort facade (P7.2 #99).

These trials pin the behavior that the annotation-only strict pass must preserve:
sticky per-shard buckets, admission/error payloads, report selection merging and
the facade names re-exported through ``proof_pool``.
"""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime import proof_pool, worker_cohort
from moex_spread_lab.proof_runtime.axis import Axis

_FACADE_NAMES = (
    "_WORKER_COHORTS",
    "_worker_init_cohort",
    "_worker_release_cohort",
    "_worker_eval_indices",
    "_worker_eval_plateau_batch",
    "_worker_report_cohort",
)


def _settings() -> ResearchSettings:
    return cast(ResearchSettings, SimpleNamespace(metadata=lambda: {"settings": "v1"}))


def _fake_case(**overrides):
    case = SimpleNamespace(
        request={"strategy": "funding_only"},
        strategy_axes={"lookback": Axis(5, 30, 1)},
        execution_axes={"quantity": Axis(1, 10, 1)},
        axes={"lookback": Axis(5, 30, 1), "quantity": Axis(1, 10, 1)},
        seed={"lookback": 5, "quantity": 1},
        split=SimpleNamespace(
            training=["2024-01-01", "2024-01-02"], metadata=lambda: {"split": "holdout"}
        ),
        fit=SimpleNamespace(measure=Mock(return_value={"rank_score": 2.0, "utility": 1.0})),
        full={
            "selection": {"strategy": "funding_only"},
            "run_id": "input-run",
            "execution_reference": True,
        },
        admission=Mock(return_value=None),
        lamp_warning=Mock(return_value=None),
        report=Mock(return_value={"run_id": "output-run", "evaluation": {"result": {}}}),
    )
    case.__dict__.update(overrides)
    return case


def _axes_patch():
    return patch.object(
        worker_cohort,
        "get_strategy_axes",
        return_value={"funding_only": {"lookback": Axis(5, 30, 1)}},
    )


def test_cohort_facade_names_stay_shared_with_proof_pool():
    # ``__all__`` is frozen to the pre-P7.2 wildcard surface; the task hooks stay shared
    # as module attributes and must not be enumerated through it.
    assert set(_FACADE_NAMES) | {"_prepare_worker_case"} <= set(vars(worker_cohort)) - {"__all__"}
    for name in _FACADE_NAMES:
        assert getattr(proof_pool, name) is getattr(worker_cohort, name)


def test_init_cohort_keeps_only_admitted_windows_and_marks_rejections():
    calendar = Mock()
    settings = _settings()
    benchmark = (["2024-01-01"], {"2024-01-01": 1.0})
    ok = _fake_case()
    rejected = _fake_case(admission=Mock(return_value="мало наблюдений"))
    failed = W0Error("нет данных")

    targets = [
        {"universe": CALENDAR_UNIVERSE, "asset": "A", "pair": "A/B"},
        {"universe": CALENDAR_UNIVERSE, "asset": "C", "pair": "C/D"},
        {"universe": "pair_screen", "asset": "E", "pair": "E/F"},
    ]
    items = [
        (
            1,
            targets[0],
            {"strategy": "funding_only", "parameters": {"lookback": 7}, "quantity": 3},
            True,
        ),
        (2, targets[1], {"strategy": "funding_only", "parameters": {"lookback": 7}}, True),
        (3, targets[2], {"strategy": "funding_only", "parameters": {"lookback": 7}}, True),
    ]
    try:
        with (
            patch.object(
                worker_cohort,
                "_worker_service",
                return_value=SimpleNamespace(calendar_data=calendar),
            ),
            _axes_patch(),
            patch.object(worker_cohort, "StudyWindow", side_effect=[ok, rejected, failed]),
        ):
            results = worker_cohort._worker_init_cohort(
                "t-init", "/root", settings, benchmark, items
            )
    finally:
        worker_cohort._worker_release_cohort("t-init")

    assert results == [
        (
            1,
            "ok",
            {
                "strategy": "funding_only",
                "strategy_axes": ok.strategy_axes,
                "execution_axes": ok.execution_axes,
                "axes": ok.axes,
                "seed": ok.seed,
                "training_first": "2024-01-01",
                "training_last": "2024-01-02",
                "training_count": 2,
                "lamp_warning": None,
            },
        ),
        (2, "error", {"reason": "мало наблюдений"}),
        (3, "error", {"reason": "нет данных"}),
    ]
    # Only calendar-universe targets are window-checked; the pair screen is skipped.
    assert calendar.check_training_window.call_args_list == [
        (("A", "A/B", 3, settings), {}),
        (("C", "C/D", 1, settings), {}),
    ]
    assert worker_cohort._WORKER_COHORTS == {}


def test_init_cohort_marks_unchecked_rejection_as_unavailable():
    rejected = _fake_case(admission=Mock(return_value="мало наблюдений"))
    target = {"universe": CALENDAR_UNIVERSE, "asset": "A", "pair": "A/B"}
    items = [(1, target, {"strategy": "funding_only", "parameters": {}}, False)]
    try:
        with (
            patch.object(
                worker_cohort, "_worker_service", return_value=SimpleNamespace(calendar_data=Mock())
            ),
            _axes_patch(),
            patch.object(worker_cohort, "StudyWindow", return_value=rejected),
        ):
            results = worker_cohort._worker_init_cohort(
                "t-unchecked", "/root", _settings(), (["2024-01-01"], {}), items
            )
        assert results == [(1, "unavailable", {"reason": "мало наблюдений"})]
        assert 1 not in worker_cohort._WORKER_COHORTS["t-unchecked"]
    finally:
        worker_cohort._worker_release_cohort("t-unchecked")


def test_report_cohort_merges_selection_and_keeps_failure_rows():
    good = _fake_case()
    out_of_range = _fake_case(
        axes={"lookback": Axis(10, 30, 1), "quantity": Axis(1, 10, 1)},
        seed={"lookback": 10, "quantity": 1},
    )
    failing = _fake_case(report=Mock(side_effect=W0Error("сбой отчёта")))
    settings = _settings()
    points = {"funding_only": {"lookback": 5, "quantity": 1}}
    worker_cohort._WORKER_COHORTS["t-report"] = cast(
        Any,
        {
            3: ({"pair": "E/F"}, failing),
            1: ({"pair": "A/B"}, good),
            2: ({"pair": "C/D"}, out_of_range),
        },
    )
    try:
        rows = worker_cohort._worker_report_cohort("t-report", points, True, settings)
    finally:
        worker_cohort._worker_release_cohort("t-report")

    assert [row[0] for row in rows] == [1, 2, 3]
    assert rows[0] == (
        1,
        {
            "target": {"pair": "A/B"},
            "selection": {
                "strategy": "funding_only",
                "quantity": 1,
                "parameters": {"lookback": 5},
            },
            "input_run_id": "input-run",
            "training_annualized_excess_pp": 2.0,
            "run_id": "output-run",
            "evaluation": {"result": {}},
        },
        True,
        good.report.return_value,
    )
    assert rows[1] == (
        2,
        {"target": {"pair": "C/D"}, "reason": "Исходные параметры не входят в общий диапазон."},
        True,
        None,
    )
    assert rows[2] == (
        3,
        {
            "target": {"pair": "E/F"},
            "selection": {
                "strategy": "funding_only",
                "quantity": 1,
                "parameters": {"lookback": 5},
            },
            "input_run_id": "input-run",
            "training_annualized_excess_pp": 2.0,
            "evaluation": {
                "settings": "v1",
                "split": "holdout",
                "execution_reference": True,
                "result": {"reason": "сбой отчёта"},
            },
        },
        True,
        None,
    )
