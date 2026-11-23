"""Isolated regressions for strict-typed cohort_reporting."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from moex_spread_lab.core.w0 import run_id
from moex_spread_lab.proof_runtime import cohort_reporting, landscape, proof_batch


def _case(strategy: str = "x") -> SimpleNamespace:
    return SimpleNamespace(
        request={"strategy": strategy},
        full={"run_id": "input"},
        seed={"window": 10},
        fit=SimpleNamespace(measure=lambda point: {"rank_score": 2.0, "utility": 2.0}),
        report=lambda point: {"run_id": "seed-output", "evaluation": {"result": {}}},
    )


def test_p72_cohort_reporting_bindings_keep_the_module_contract() -> None:
    assert proof_batch._unavailable is cohort_reporting.unavailable
    assert cohort_reporting.unavailable.__module__ == cohort_reporting.__name__
    assert callable(cohort_reporting._lamps)


def test_p72_cohort_reporting_progress_events_keep_the_percentage_bands() -> None:
    events: list[dict[str, Any]] = []
    point = {"window": 10}
    evaluation = {"result": {"annualized_excess_pp": 3.0}}
    rows = [{"target": {"pair": "A/B"}, "evaluation": evaluation}]
    single = {"run_id": "output", "evaluation": evaluation}

    def report_members(
        point_arg: dict[str, Any],
        best: dict[str, Any] | None,
        _settings: Any,
        _check_cancelled: Any,
        *,
        on_progress: Any,
    ) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
        assert point_arg == best == point
        on_progress(2, 2)
        return rows, False, single

    group = SimpleNamespace(
        axes={"window": object()},
        seed=point,
        members=[({}, _case())],
        land=SimpleNamespace(measure=lambda values: {"rank_score": 3.0}),
        active="x",
        report_members=report_members,
        plateau=lambda point_arg, *, on_progress: (on_progress(1, 1), {"green": 70})[1],
    )
    result = cohort_reporting.build_cohort_result(
        cohort_reporting.CohortReport(
            {"strategy": "x", "targets": [{}]},
            SimpleNamespace(
                random_seed=41, validation_percent=25, metadata=lambda: {"validation_percent": 25}
            ),
            group,
            point,
            1,
            [point],
            lambda: None,
            events.append,
        )
    )
    assert [(event["stage"], event["percent"]) for event in events] == [
        ("report", 88.0),
        ("report", 94.0),
        ("plateau", 94.0),
        ("plateau", 99.0),
    ]
    assert result["group"]["result_mean_pp"] == 3.0
    assert result["run_id"] == run_id({k: v for k, v in result.items() if k != "run_id"})


def test_p72_cohort_reporting_missing_member_rate_is_not_a_zero() -> None:
    point = {"window": 10}
    rows = [
        {"target": {"pair": "A/B"}, "evaluation": {"result": {"annualized_excess_pp": 4.0}}},
        {"target": {"pair": "C/D"}, "evaluation": {"result": {"reason": "no_trades"}}},
    ]
    group = SimpleNamespace(
        axes={"window": object()},
        seed=point,
        members=[({}, _case("x"))],
        land=SimpleNamespace(measure=lambda values: {"rank_score": 3.0}),
        active="other",
        report_members=lambda *args, on_progress=None, **kwargs: (rows, True, None),
        plateau=lambda point_arg, *, on_progress: None,
    )
    result = cohort_reporting.build_cohort_result(
        cohort_reporting.CohortReport(
            {"strategy": "x"},
            SimpleNamespace(random_seed=7, validation_percent=0.0, metadata=dict),
            group,
            point,
            2,
            [point, point],
            lambda: None,
            None,
        )
    )
    assert result["group"]["member_count"] == 2
    assert result["group"]["result_mean_pp"] is None
    assert result["group"]["execution_reference"] is True
    assert result["plateau"] is None
    assert "input_run_id" not in result


def test_p72_cohort_reporting_unavailable_report_keeps_run_id_and_lamps() -> None:
    fit = landscape.Landscape({"window": SimpleNamespace(step=1, contains=lambda v: True)}, None)
    fit.cache[("window",)] = {"rank_score": 1.0, "utility": 1.0}
    case = _case("native")
    case.fit = fit
    result = cohort_reporting.unavailable(cast(Any, case), "недостаточно сделок")
    assert result["status"] == "unavailable"
    assert result["reason"] == "недостаточно сделок"
    assert result["input_run_id"] == "input"
    assert result["plateau"]["basis"] == "training"
    assert result["run_id"] == run_id({k: v for k, v in result.items() if k != "run_id"})
    assert sorted(result["plateau"]) == ["amber", "basis", "fields", "green", "unit"]
