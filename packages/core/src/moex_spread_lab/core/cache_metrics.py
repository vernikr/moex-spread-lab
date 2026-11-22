"""Request-local, opt-in cache telemetry for diagnostic probes."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

_COUNTERS: ContextVar[dict[str, int] | None] = ContextVar("msl_cache_metrics", default=None)


def record_cache_lookup(name: str, hit: bool) -> None:
    counters = _COUNTERS.get()
    if counters is not None:
        key = f"{name}_{'hits' if hit else 'misses'}"
        counters[key] = counters.get(key, 0) + 1


@contextmanager
def collect_cache_metrics() -> Generator[dict[str, int]]:
    counters = {"json_hits": 0, "json_misses": 0, "zscore_hits": 0, "zscore_misses": 0}
    token = _COUNTERS.set(counters)
    try:
        yield counters
    finally:
        _COUNTERS.reset(token)
