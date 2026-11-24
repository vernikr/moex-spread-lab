"""Probe-only process telemetry preserves worker results and task isolation."""

import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from moex_spread_lab.core.cache_metrics import record_cache_lookup
from moex_spread_lab.proof.worker_metrics import (
    collect_worker_metrics,
    submit_worker,
    unwrap_worker_result,
)


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
