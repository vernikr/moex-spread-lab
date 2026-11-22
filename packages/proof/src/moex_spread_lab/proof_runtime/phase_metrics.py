"""Opt-in, request-context wall timings around explicit proof operations."""

from __future__ import annotations

import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")
_METRICS: ContextVar[dict[str, float] | None] = ContextVar("msl_phase_metrics", default=None)
_ACTIVE: ContextVar[frozenset[str]] = ContextVar("msl_phase_active", default=frozenset())


@contextmanager
def collect_phase_metrics() -> Generator[dict[str, float]]:
    timings: dict[str, float] = {}
    token = _METRICS.set(timings)
    active_token = _ACTIVE.set(frozenset())
    try:
        yield timings
    finally:
        _ACTIVE.reset(active_token)
        _METRICS.reset(token)


def measure_phase(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        @wraps(fn)
        def call(*args: P.args, **kwargs: P.kwargs) -> R:
            timings = _METRICS.get()
            active = _ACTIVE.get()
            if timings is None or name in active:
                return fn(*args, **kwargs)
            token = _ACTIVE.set(active | {name})
            started = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                timings[name] = timings.get(name, 0.0) + (time.perf_counter() - started) * 1000
                _ACTIVE.reset(token)

        return call

    return decorate
