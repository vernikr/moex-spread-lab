"""Opt-in task-local worker telemetry transported with executor results."""

from __future__ import annotations

import os
import resource
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from moex_spread_lab.core.derived_cache import collect_cache_metrics

_CACHE_KEYS = ("json_hits", "json_misses", "zscore_hits", "zscore_misses")


@dataclass
class WorkerTotals:
    cache: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_CACHE_KEYS, 0))
    pids: set[int] = field(default_factory=set)
    tasks: int = 0
    user_ms: float = 0.0
    sys_ms: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "tasks": self.tasks,
            "workers": len(self.pids),
            "cache": dict(self.cache),
            "cpu_user_ms": round(self.user_ms, 2),
            "cpu_sys_ms": round(self.sys_ms, 2),
        }


@dataclass
class WorkerEnvelope:
    result: Any
    cache: dict[str, int]
    pid: int
    user_ms: float
    sys_ms: float


_CURRENT: ContextVar[WorkerTotals | None] = ContextVar("msl_worker_metrics", default=None)


@contextmanager
def collect_worker_metrics() -> Generator[WorkerTotals]:
    totals = WorkerTotals()
    token = _CURRENT.set(totals)
    try:
        yield totals
    finally:
        _CURRENT.reset(token)


def _worker_call(fn: Callable[..., Any], args: tuple[Any, ...]) -> WorkerEnvelope:
    before = resource.getrusage(resource.RUSAGE_SELF)
    with collect_cache_metrics() as cache:
        result = fn(*args)
    after = resource.getrusage(resource.RUSAGE_SELF)
    return WorkerEnvelope(
        result,
        cache,
        os.getpid(),
        (after.ru_utime - before.ru_utime) * 1000,
        (after.ru_stime - before.ru_stime) * 1000,
    )


def submit_worker(executor: Any, fn: Callable[..., Any], *args: Any) -> Any:
    if _CURRENT.get() is None:
        return executor.submit(fn, *args)
    return executor.submit(_worker_call, fn, args)


def unwrap_worker_result(value: Any) -> Any:
    totals = _CURRENT.get()
    if totals is None or not isinstance(value, WorkerEnvelope):
        return value
    totals.tasks += 1
    totals.pids.add(value.pid)
    totals.user_ms += value.user_ms
    totals.sys_ms += value.sys_ms
    for name, count in value.cache.items():
        totals.cache[name] += count
    return value.result


_P = ParamSpec("_P")
_R = TypeVar("_R")
_PHASE_METRICS: ContextVar[dict[str, float] | None] = ContextVar("msl_phase_metrics", default=None)
_PHASE_ACTIVE: ContextVar[frozenset[str]] = ContextVar("msl_phase_active", default=frozenset())


@contextmanager
def collect_phase_metrics() -> Generator[dict[str, float]]:
    timings: dict[str, float] = {}
    token = _PHASE_METRICS.set(timings)
    active_token = _PHASE_ACTIVE.set(frozenset())
    try:
        yield timings
    finally:
        _PHASE_ACTIVE.reset(active_token)
        _PHASE_METRICS.reset(token)


def measure_phase(name: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    def decorate(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(fn)
        def call(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            timings = _PHASE_METRICS.get()
            active = _PHASE_ACTIVE.get()
            if timings is None or name in active:
                return fn(*args, **kwargs)
            token = _PHASE_ACTIVE.set(active | {name})
            started = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                timings[name] = timings.get(name, 0.0) + (time.perf_counter() - started) * 1000
                _PHASE_ACTIVE.reset(token)

        return call

    return decorate
