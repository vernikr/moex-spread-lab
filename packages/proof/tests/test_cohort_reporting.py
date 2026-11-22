"""Cohort report assembly and compatibility-facade contracts."""

from types import SimpleNamespace
from unittest.mock import Mock

from moex_spread_lab.core.w0 import _run_id
from moex_spread_lab.proof_runtime import cohort_reporting, proof_batch


def test_report_keeps_run_id_progress_and_single_window_details():
    point = {"window": 10}
    evaluation = {"result": {"annualized_excess_pp": 3.0}}
    first = SimpleNamespace(request={"strategy": "x"}, full={"run_id": "input"})
    rows = [{"target": {"pair": "A/B"}, "evaluation": evaluation}]
    single = {"run_id": "output", "evaluation": evaluation}
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
