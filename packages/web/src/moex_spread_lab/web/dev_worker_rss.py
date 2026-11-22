"""Optional best-effort pool RSS sampling during diagnostic requests."""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from .dev_pool_metrics import pool_snapshot


class WorkerRSSSampler:
    """Peak observed aggregate pool RSS, not an exact instantaneous maximum."""

    def __init__(self, store: Any, interval: float = 0.1):
        self.store = store
        self.interval = interval
        self.samples = 0
        self.incomplete = 0
        self.peak_kb: int | None = None
        self.max_workers = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="probe-worker-rss")

    def _sample(self) -> None:
        try:
            pool = pool_snapshot(self.store, resources=True)
            resource = pool["worker_resources"]
            if not resource["complete"] or resource["rss_kb"] is None:
                self.incomplete += 1
                return
            self.samples += 1
            rss = int(resource["rss_kb"])
            self.peak_kb = max(self.peak_kb if self.peak_kb is not None else 0, rss)
            self.max_workers = max(self.max_workers, resource["sampled_workers"])
        except (OSError, RuntimeError, KeyError, TypeError, ValueError):
            self.incomplete += 1

    def _run(self) -> None:
        while not self.stop.is_set():
            self._sample()
            self.stop.wait(self.interval)

    def snapshot(self) -> dict[str, Any]:
        return {
            "sample_interval_ms": round(self.interval * 1000),
            "samples": self.samples,
            "incomplete_samples": self.incomplete,
            "max_workers_sampled": self.max_workers,
            "sampler_stopped": not self.thread.is_alive(),
            "peak_sampled_pool_rss_kb": self.peak_kb,
        }


@contextmanager
def sample_worker_rss(store: Any, enabled: bool) -> Generator[WorkerRSSSampler | None]:
    if not enabled:
        yield None
        return
    sampler = WorkerRSSSampler(store)
    sampler.thread.start()
    try:
        yield sampler
    finally:
        sampler.stop.set()
        sampler.thread.join(timeout=3)
        if not sampler.thread.is_alive():
            sampler._sample()
