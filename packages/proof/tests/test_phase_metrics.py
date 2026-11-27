"""Request-local phase timing wraps operations without double-counting recursion."""

import inspect
from concurrent.futures import ThreadPoolExecutor

import pytest

from moex_spread_lab.proof import worker_metrics as metrics


def test_same_phase_nesting_and_exceptions_are_timed_once(monkeypatch):
    clock = iter([10.0, 10.1, 10.2, 10.4])
    monkeypatch.setattr(metrics.time, "perf_counter", lambda: next(clock))

    @metrics.measure_phase("search")
    def inner(value):
        return value

    @metrics.measure_phase("search")
    def outer(value):
        return inner(value)

    @metrics.measure_phase("plateau")
    def fail():
        raise ValueError("expected")

    assert list(inspect.signature(outer).parameters) == ["value"]
    with metrics.collect_phase_metrics() as result:
        assert outer(3) == 3
        with pytest.raises(ValueError):
            fail()
    assert result["search"] == pytest.approx(100)
    assert result["plateau"] == pytest.approx(200)
    assert outer(4) == 4  # Disabled mode does not use the clock.


def test_timings_do_not_leak_into_other_threads():
    @metrics.measure_phase("work")
    def work():
        return True

    with metrics.collect_phase_metrics() as result, ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(work).result()
        assert result == {}
        assert work()
        assert "work" in result
