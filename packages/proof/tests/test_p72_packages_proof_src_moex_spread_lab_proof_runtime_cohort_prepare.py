"""Isolated regressions for strict-typed cohort_prepare."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.proof_runtime import (
    cohort_models,
    cohort_prepare,
    cohort_prepare_helpers,
    remote_cohort_types,
    request_validation,
)
from moex_spread_lab.proof_runtime.axis import Axis
from moex_spread_lab.proof_runtime.cohort_prepare import PreparationInputs, prepare_cohort
from moex_spread_lab.strategies.funding_carry.carry import CarryStrategyError


def _fake_settings() -> SimpleNamespace:
    return SimpleNamespace(
        price_fill="reference_same_bar",
        timeframe="1d",
        metadata=lambda: {"validation_percent": 25.0, "timeframe": "1d"},
    )


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_bindings() -> None:
    assert cohort_prepare.execution_axes is request_validation.execution_axes
    assert cohort_prepare._Cohort is cohort_models._Cohort
    assert cohort_prepare._RemoteCohort is cohort_models._RemoteCohort
    assert cohort_prepare._RemoteCase is remote_cohort_types._RemoteCase
    assert cohort_prepare._RemoteSplit is remote_cohort_types._RemoteSplit
    assert cohort_prepare._TrainingSlice is remote_cohort_types._TrainingSlice
    assert cohort_prepare._targets is cohort_prepare_helpers._targets
    assert cohort_prepare._translate is cohort_prepare_helpers._translate


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_local() -> None:
    seen_initial: list[dict[str, Any]] = []
    progress_calls: list[tuple[int, int]] = []
    target_a = {"universe": "pair_screen", "pair": "AAA/BBB"}
    target_b = {"universe": "pair_screen", "pair": "CCC/DDD"}

    def window_factory(
        _svc: Any, req: dict[str, Any], _settings: Any, _bm: Any, _cal: Any
    ) -> SimpleNamespace:
        seen_initial.append(dict(req["parameters"]))
        if req["pair"] == "AAA/BBB" and req["parameters"]["lookback"] != 5:
            raise CarryStrategyError("warmup")
        axes = {"lookback": Axis(5, 25, 1)}
        return SimpleNamespace(
            request={"strategy": "funding_only"},
            strategy_axes=axes,
            split=SimpleNamespace(training=list(range(20))),
            admission=lambda: None,
            fit=SimpleNamespace(
                measure=lambda pt: {
                    "rank_score": 2.0 if pt["lookback"] == 5 else None,
                    "utility": 2.0 if pt["lookback"] == 5 else None,
                    "reason": "no_trades",
                }
            ),
        )

    cohort = prepare_cohort(
        PreparationInputs(
            service=SimpleNamespace(root="/tmp"),
            request={
                "targets": [target_a, target_b],
                "strategy": "funding_only",
                "sizing": AUTO_SIZING,
                "parameters": {"lookback": 35},
            },
            settings=_fake_settings(),
            benchmark=([], {}),
            check_cancelled=lambda: None,
            calendar=SimpleNamespace(),
            on_progress=lambda done, total: progress_calls.append((done, total)),
            registry=None,
            window_factory=window_factory,
            can_use_pool=lambda *_args: False,
            pool_factory=lambda _svc: None,
            unavailable=lambda _case, reason: {"status": "unavailable", "reason": reason},
        )
    )
    assert isinstance(cohort, cohort_models._Cohort)
    assert cohort.axes == {"lookback": Axis(5, 25, 1)}
    assert seen_initial == [{"lookback": 35}, {"lookback": 5}, {"lookback": 35}]
    assert progress_calls == [(1, 2), (2, 2)]
    assert cohort.land.measure({"lookback": 5})["utility"] == 2.0
    assert cohort.land.measure({"lookback": 6})["rank_score"] is None


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_unchecked_unavailable() -> (
    None
):
    cohort = prepare_cohort(
        PreparationInputs(
            service=SimpleNamespace(root="/tmp"),
            request={
                "universe": "pair_screen",
                "pair": "AAA/BBB",
                "strategy": "funding_only",
                "sizing": AUTO_SIZING,
            },
            settings=_fake_settings(),
            benchmark=([], {}),
            check_cancelled=lambda: None,
            calendar=SimpleNamespace(),
            on_progress=None,
            registry=None,
            window_factory=lambda *_args: SimpleNamespace(
                admission=lambda: "недостаточно сделок",
                strategy_axes={"lookback": Axis(5, 40, 1)},
            ),
            can_use_pool=lambda *_args: False,
            pool_factory=lambda _svc: None,
            unavailable=lambda _case, reason: {"status": "unavailable", "reason": reason},
        )
    )
    assert cohort == {"status": "unavailable", "reason": "недостаточно сделок"}


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_remote_and_reject() -> (
    None
):
    released: list[tuple[str, Any]] = []
    target = {"universe": "pair_screen", "pair": "AAA/BBB"}

    class FakePool:
        def init_cohort(
            self,
            _root: Any,
            _settings: Any,
            _benchmark: Any,
            items: list[tuple[int, dict[str, Any], dict[str, Any], bool]],
            _check_cancelled: Any,
            *,
            on_progress: Any = None,
        ) -> dict[str, Any]:
            assert len(items) == 2
            return {
                "cohort_id": "cid-1",
                "owner_of": {0: 0, 1: 0},
                "results": [
                    (
                        0,
                        "ok",
                        {
                            "strategy": "mean_reversion",
                            "strategy_axes": {
                                "lookback": Axis(6, 30, 1),
                                "entry_z": Axis(0.5, 3.0, 0.1),
                                "exit_z": Axis(0.1, 2.5, 0.1),
                            },
                            "execution_axes": {},
                            "axes": {
                                "lookback": Axis(6, 30, 1),
                                "entry_z": Axis(0.5, 3.0, 0.1),
                                "exit_z": Axis(0.1, 2.5, 0.1),
                            },
                            "seed": {"lookback": 10, "entry_z": 1.5, "exit_z": 0.5},
                            "training_first": "2024-01-01",
                            "training_last": "2024-03-01",
                            "training_count": 20,
                            "lamp_warning": None,
                        },
                    ),
                    (1, "error", {"reason": "bad_window"}),
                ],
            }

        def release_cohort(self, cohort_id: str, owner_of: Any) -> None:
            released.append((cohort_id, owner_of))

        def eval_point(
            self,
            _state: dict[str, Any],
            point_by_strat: dict[str, dict[str, Any]],
            _canary: list[int],
            total_members: int,
            _check_cancelled: Any,
        ) -> dict[str, Any]:
            return {"rank_score": 1.5, "utility": 1.5, "seen": (point_by_strat, total_members)}

    # Rejected remote member returns confirmation and releases cohort
    rejected_result = prepare_cohort(
        PreparationInputs(
            service=SimpleNamespace(root="/tmp"),
            request={
                "targets": [target, {"universe": "pair_screen", "pair": "ZZZ/WWW"}],
                "strategy": "mean_reversion",
                "sizing": AUTO_SIZING,
            },
            settings=_fake_settings(),
            benchmark=([], {}),
            check_cancelled=lambda: None,
            calendar=SimpleNamespace(),
            on_progress=None,
            registry=None,
            window_factory=lambda *_args: None,
            can_use_pool=lambda *_args: True,
            pool_factory=lambda _svc: FakePool(),
            unavailable=lambda *_args: {},
        )
    )
    assert isinstance(rejected_result, dict)
    assert rejected_result["status"] == "selection_requires_confirmation"
    assert rejected_result["remaining_count"] == 1
    assert released == [("cid-1", {0: 0, 1: 0})]

    # Passing remote cohort translates zscore_mean_reversion -> mean_reversion and measures
    class OkPool(FakePool):
        def init_cohort(
            self,
            _root: Any,
            _settings: Any,
            _benchmark: Any,
            items: list[tuple[int, dict[str, Any], dict[str, Any], bool]],
            _check_cancelled: Any,
            *,
            on_progress: Any = None,
        ) -> dict[str, Any]:
            return {
                "cohort_id": "cid-2",
                "owner_of": {0: 0},
                "results": [
                    (
                        0,
                        "ok",
                        {
                            "strategy": "mean_reversion",
                            "strategy_axes": {
                                "lookback": Axis(6, 30, 1),
                                "entry_z": Axis(0.5, 3.0, 0.1),
                                "exit_z": Axis(0.1, 2.5, 0.1),
                            },
                            "execution_axes": {},
                            "axes": {
                                "lookback": Axis(6, 30, 1),
                                "entry_z": Axis(0.5, 3.0, 0.1),
                                "exit_z": Axis(0.1, 2.5, 0.1),
                            },
                            "seed": {"lookback": 10, "entry_z": 1.5, "exit_z": 0.5},
                            "training_first": "2024-01-01",
                            "training_last": "2024-03-01",
                            "training_count": 20,
                            "lamp_warning": None,
                        },
                    )
                ],
            }

    remote = prepare_cohort(
        PreparationInputs(
            service=SimpleNamespace(root="/tmp"),
            request={
                "targets": [target],
                "strategy": "zscore_mean_reversion",
                "sizing": AUTO_SIZING,
            },
            settings=_fake_settings(),
            benchmark=([], {}),
            check_cancelled=lambda: None,
            calendar=SimpleNamespace(),
            on_progress=None,
            registry=None,
            window_factory=lambda *_args: None,
            can_use_pool=lambda *_args: True,
            pool_factory=lambda _svc: OkPool(),
            unavailable=lambda *_args: {},
        )
    )
    assert isinstance(remote, cohort_models._RemoteCohort)
    assert remote.axes["window"] == Axis(6, 30, 1)
    measured = remote.land.measure({"window": 10, "entry_z": 1.5, "exit_z": 0.5})
    assert measured["seen"] == (
        {"mean_reversion": {"lookback": 10, "entry_z": 1.5, "exit_z": 0.5}},
        1,
    )

    # Early-rejected target (LK lacks RUONIA convergence) produces confirmation in local path
    lk_target = {"universe": "calendar_native", "asset": "LK", "pair": "LKH6/LKM6"}
    sr_target = {"universe": "calendar_native", "asset": "SR", "pair": "SRH6/SRM6"}
    early = prepare_cohort(
        PreparationInputs(
            service=SimpleNamespace(root="/tmp"),
            request={
                "targets": [lk_target, sr_target],
                "strategy": "convergence_to_ruonia",
                "sizing": AUTO_SIZING,
            },
            settings=_fake_settings(),
            benchmark=([], {}),
            check_cancelled=lambda: None,
            calendar=SimpleNamespace(check_training_window=lambda *_args: None),
            on_progress=None,
            registry=None,
            window_factory=lambda *_args: SimpleNamespace(
                request={"strategy": "convergence_to_ruonia"},
                strategy_axes={},
                split=SimpleNamespace(training=list(range(20))),
                admission=lambda: None,
            ),
            can_use_pool=lambda *_args: False,
            pool_factory=lambda _svc: None,
            unavailable=lambda *_args: {},
        )
    )
    assert isinstance(early, dict)
    assert early["status"] == "selection_requires_confirmation"
    assert early["remaining_count"] == 1
    assert early["excluded"][0]["target"] == lk_target
