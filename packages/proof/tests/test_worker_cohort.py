"""Worker pool runtime: cohort extraction keeps sticky state, ordering and retry
rules; shard weights keep sealed/legacy precedence; process telemetry keeps
task isolation and request-local phase timing."""

import inspect
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

import pytest

from moex_spread_lab.core.cache_metrics import record_cache_lookup
from moex_spread_lab.proof import proof_pool, worker_cohort, worker_metrics
from moex_spread_lab.proof.landscape import Axis
from moex_spread_lab.proof.worker_metrics import (
    collect_worker_metrics,
    submit_worker,
    unwrap_worker_result,
)
from moex_spread_lab.strategies.funding_carry.study import CarryStrategyError


def test_worker_exports_and_carry_retry_keep_shared_state():
    for name in (
        "worker_init_cohort",
        "worker_release_cohort",
        "worker_eval_indices",
        "worker_eval_plateau_batch",
        "worker_report_cohort",
    ):
        assert getattr(proof_pool, name) is getattr(worker_cohort, name)
    native = {"strategy": "funding_only", "parameters": {"lookback": 99}}
    case = object()
    with (
        patch.object(
            worker_cohort,
            "get_strategy_axes",
            return_value={"funding_only": {"lookback": Axis(5, 30, 1)}},
        ),
        patch.object(
            worker_cohort, "StudyWindow", side_effect=[CarryStrategyError("history"), case]
        ) as constructor,
    ):
        assert (
            worker_cohort._prepare_worker_case(None, native, *cast(Any, (None, None, None))) is case
        )
    assert constructor.call_args_list[0].args[1]["parameters"] == {"lookback": 30}
    assert constructor.call_args_list[1].args[1]["parameters"] == {"lookback": 5}
    assert native["parameters"] == {"lookback": 99}
    with (
        patch.object(
            worker_cohort,
            "get_strategy_axes",
            return_value={"funding_only": {"lookback": Axis(5, 30, 1)}},
        ),
        patch.object(
            worker_cohort, "StudyWindow", side_effect=CarryStrategyError("history")
        ) as constructor,
        pytest.raises(CarryStrategyError),
    ):
        worker_cohort._prepare_worker_case(
            None, {**native, "parameters": {"lookback": 5}}, *cast(Any, (None, None, None))
        )
    assert constructor.call_count == 1


def test_worker_evaluation_is_sorted_and_stops_at_first_failure():
    first = SimpleNamespace(
        request={"strategy": "x"},
        fit=SimpleNamespace(measure=Mock(return_value={"rank_score": 2, "utility": 2})),
    )
    second = SimpleNamespace(
        request={"strategy": "x"},
        fit=SimpleNamespace(
            measure=Mock(return_value={"rank_score": None, "utility": None, "reason": "bad"})
        ),
    )
    worker_cohort._WORKER_COHORTS["test"] = cast(
        Any, {2: ({"pair": "B"}, second), 1: ({"pair": "A"}, first)}
    )
    try:
        points = {"x": {"window": 5}}
        expected = ((2, "B", "bad"), [(1, 2.0, True)])
        assert worker_cohort.worker_eval_indices("test", points) == expected
        assert worker_cohort.worker_eval_plateau_batch("test", [(7, points)]) == {7: expected}
        assert worker_cohort.worker_eval_indices("test", points, exclude_idx=2) == (
            None,
            [(1, 2.0, True)],
        )
    finally:
        worker_cohort.worker_release_cohort("test")
    assert "test" not in worker_cohort._WORKER_COHORTS


# ---- worker shard weights (folded into the worker pool suite) ----


def test_unsealed_weights_use_screen_then_legacy_without_reopening_seal(tmp_path):
    target = {"universe": "pair_screen", "pair": "FAR/NEAR"}
    cache = {}
    screen = tmp_path / "data/w1/candles-screen/NEAR_1h.json"
    legacy = tmp_path / "data/w1/candles/NEAR_1h.json"
    screen.parent.mkdir(parents=True)
    legacy.parent.mkdir(parents=True)
    screen.write_bytes(b"x" * (128 * 200))
    legacy.write_bytes(b"x" * (128 * 300))
    with patch.object(proof_pool, "SealedH1Reader") as reader:
        reader.return_value.payload.return_value = None
        assert proof_pool._estimate_weight(tmp_path, target, "1h", _seal_cache=cache) == 200
        screen.unlink()
        assert proof_pool._estimate_weight(tmp_path, target, "1h", _seal_cache=cache) == 300
        legacy.unlink()
        assert proof_pool._estimate_weight(tmp_path, target, "1h", _seal_cache=cache) == 1000
        assert reader.call_count == 1
        assert reader.return_value.payload.call_count == 1
    assert proof_pool._estimate_weight is proof_pool.estimate_weight


def test_sealed_weight_uses_logical_bytes_not_present_legacy_file(tmp_path):
    target = {"universe": "pair_screen", "pair": "FAR/NEAR"}
    legacy = tmp_path / "data/w1/candles-screen/NEAR_1h.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"x" * (128 * 500))
    cache = {}
    with patch.object(proof_pool, "SealedH1Reader") as factory:
        reader = factory.return_value
        reader.root = tmp_path
        reader.select.return_value = legacy
        reader.payload.return_value = {
            "plan": {
                "files": [
                    {
                        "legacy_path": legacy.relative_to(tmp_path).as_posix(),
                        "logical_bytes": 128 * 250,
                    }
                ]
            }
        }
        assert proof_pool.estimate_weight(tmp_path, target, "1h", _seal_cache=cache) == 250
        reader.select.return_value = tmp_path / "missing.json"
        assert proof_pool.estimate_weight(tmp_path, target, "1h", _seal_cache=cache) == 1000
        assert factory.call_count == 1


def test_weight_defaults_do_not_read_seal(tmp_path):
    with patch.object(proof_pool, "SealedH1Reader") as reader:
        assert proof_pool.estimate_weight(tmp_path, {}, "1d") == 200
        assert proof_pool.estimate_weight(tmp_path, {}, "1h") == 1000
        assert (
            proof_pool.estimate_weight(
                tmp_path, {"universe": "pair_screen", "pair": "invalid"}, "1h"
            )
            == 1000
        )
        reader.assert_not_called()


# ---- probe-only process telemetry (folded into the worker pool suite) ----


def test_real_spawn_workers_return_scoped_cache_and_cpu():
    with ProcessPoolExecutor(
        max_workers=1, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        assert submit_worker(pool, sum, [1, 2]).result() == 3
        with collect_worker_metrics() as totals:
            future = submit_worker(pool, record_cache_lookup, "zscore", True)
            assert unwrap_worker_result(future.result()) is None
            future = submit_worker(pool, record_cache_lookup, "json", False)
            assert unwrap_worker_result(future.result()) is None
            result = totals.snapshot()
        assert result["tasks"] == 2 and result["workers"] == 1
        assert result["cache"]["zscore_hits"] == 1 and result["cache"]["json_misses"] == 1
        assert result["cpu_user_ms"] >= 0 and result["cpu_sys_ms"] >= 0
        assert submit_worker(pool, sum, [5]).result() == 5
        with collect_worker_metrics() as other:
            assert unwrap_worker_result(submit_worker(pool, sum, [3]).result()) == 3
            assert other.snapshot()["cache"]["zscore_hits"] == 0


def test_submission_in_other_thread_does_not_inherit_probe_context():
    class Executor:
        def submit(self, fn, *args):
            return fn(*args)

    with collect_worker_metrics() as totals, ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(submit_worker, Executor(), sum, [3]).result() == 3
        assert totals.snapshot()["tasks"] == 0


# ---- request-local phase timing (folded into the worker pool suite) ----


def test_same_phase_nesting_and_exceptions_are_timed_once(monkeypatch):
    clock = iter([10.0, 10.1, 10.2, 10.4])
    monkeypatch.setattr(worker_metrics.time, "perf_counter", lambda: next(clock))

    @worker_metrics.measure_phase("search")
    def inner(value):
        return value

    @worker_metrics.measure_phase("search")
    def outer(value):
        return inner(value)

    @worker_metrics.measure_phase("plateau")
    def fail():
        raise ValueError("expected")

    assert list(inspect.signature(outer).parameters) == ["value"]
    with worker_metrics.collect_phase_metrics() as result:
        assert outer(3) == 3
        with pytest.raises(ValueError):
            fail()
    assert result["search"] == pytest.approx(100)
    assert result["plateau"] == pytest.approx(200)
    assert outer(4) == 4  # Disabled mode does not use the clock.


def test_timings_do_not_leak_into_other_threads():
    @worker_metrics.measure_phase("work")
    def work():
        return True

    with (
        worker_metrics.collect_phase_metrics() as result,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        assert pool.submit(work).result()
        assert result == {}
        assert work()
        assert "work" in result
