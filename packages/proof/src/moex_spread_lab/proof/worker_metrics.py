"""Opt-in task-local worker telemetry transported with executor results."""

from __future__ import annotations

import os
import resource
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from moex_spread_lab.core.cache_metrics import collect_cache_metrics

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
