"""Extracted cohort models preserve prewarmed probes and lifecycle contracts."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

from moex_spread_lab.proof import cohort_models, proof_batch
from moex_spread_lab.proof.landscape import Axis, Landscape


def test_remote_plateau_prewarm_deduplicates_and_release_is_idempotent():
    axes = {"lookback": Axis(3, 7, 1)}
    land = Landscape(axes, lambda _: {"utility": 10.0})
    case = SimpleNamespace(request={"strategy": "mean_reversion"}, lamp_warning=lambda: None)
    pool = Mock()

    def batch(state, payload, check_cancelled, *, on_progress):
        assert [p[1]["mean_reversion"]["lookback"] for p in payload] == [5, 3, 4, 6, 7]
        on_progress(1, 1)
        return {i: {"utility": 10.0} for i, _ in payload}

    pool.eval_plateau_batch.side_effect = batch
    state = {"cohort_id": "id", "owner_of": {0: 0}}
    group = cohort_models.RemoteCohort(
        active="mean_reversion",
        axes=axes,
        seed={"lookback": 5},
        members=[({}, cast(Any, case))],
        land=land,
        pool=pool,
        cohort_state=state,
        distinct_strategies=("mean_reversion",),
        check_cancelled=lambda: None,
    )
    events = []
    result = group.plateau(
        {"lookback": 5}, on_progress=lambda done, total: events.append((done, total))
    )
    assert result["fields"]["lookback"]["score"] == 100.0
    assert events == [(1, 1)] and len(land.cache) == 5
    assert group.plateau({"lookback": 5}) == result
    assert pool.eval_plateau_batch.call_count == 1
    group.close()
    group.close()
    pool.release_cohort.assert_called_once_with("id", {0: 0})
    assert proof_batch._Cohort is cohort_models.Cohort
    assert proof_batch._RemoteCohort is cohort_models.RemoteCohort


def test_invalid_center_does_not_submit_plateau_work():
    axes = {"lookback": Axis(3, 7, 1)}
    land = Landscape(axes, lambda _: {"utility": 10.0})
    case = SimpleNamespace(request={"strategy": "mean_reversion"}, lamp_warning=lambda: None)
    pool = Mock()
    group = cohort_models.RemoteCohort(
        active="mean_reversion",
        axes=axes,
        seed={"lookback": 5},
        members=[({}, cast(Any, case))],
        land=land,
        pool=pool,
        cohort_state={},
        distinct_strategies=("mean_reversion",),
        check_cancelled=lambda: None,
    )
    field = group.plateau({"lookback": 9})["fields"]["lookback"]
    assert field["score"] is None and "диапазон" in field["reason"]
    pool.eval_plateau_batch.assert_not_called()
