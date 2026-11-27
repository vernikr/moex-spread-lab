"""Read-only loopback diagnostics; never persist results or initialize worker pools."""

from __future__ import annotations

import cProfile
import io
import json
import math
import os
import pstats
import resource
import statistics
import subprocess
import sys
import sysconfig
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from itertools import chain, islice
from pathlib import Path
from typing import Any

from moex_spread_lab.core import _fastcore_loader
from moex_spread_lab.core.cache_metrics import collect_cache_metrics
from moex_spread_lab.core.settings import settings_with_override
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.proof_batch import checked_plateau
from moex_spread_lab.proof.proof_pool import configured_workers
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.proof_windows import window_summaries
from moex_spread_lab.proof.worker_metrics import collect_phase_metrics, collect_worker_metrics

_STARTED = time.monotonic()
_STARTED_AT = datetime.now(UTC).isoformat()


def fastcore_binary(root: Path) -> tuple[bool, str | None]:
    """Report the normal binary's presence and privacy-preserving location, not load state."""
    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    path = Path(_fastcore_loader.__file__).resolve().with_name("_fastcore" + suffix)
    if not path.is_file():
        return False, None
    try:
        return True, "$ROOT/" + path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return True, "$EXTERNAL/" + path.name


def calendar_cache_count(root: Path) -> int | None:
    """Count regular JSON files directly in the calendar EOD cache; None means unreadable."""
    try:
        with os.scandir(root / "data/spread-history-cache") as entries:
            return sum(entry.name.endswith(".json") and entry.is_file() for entry in entries)
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def profile_summary(profiler: cProfile.Profile) -> str:
    """Top 20 cumulative functions, retaining all rows and bounding each line."""
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(20)
    return "\n".join(line[:240] for line in stream.getvalue().splitlines()) + "\n"


def _cpu_seconds(value: str) -> float:
    days = 0
    if "-" in value:
        raw_days, value = value.split("-", 1)
        days = int(raw_days)
    parts = [float(part) for part in value.split(":")]
    if len(parts) not in (2, 3):
        raise ValueError("unexpected ps time")
    total = 0.0
    for part in parts:
        total = total * 60 + part
    return total + days * 86400


def worker_resources(pids: set[int]) -> dict[str, int | float | bool | None]:
    result: dict[str, int | float | bool | None] = {
        "sampled_workers": 0,
        "rss_kb": 0,
        "cpu_lifetime_ms": 0,
        "complete": True,
    }
    if pids:
        result.update(rss_kb=None, cpu_lifetime_ms=None, complete=False)
        try:
            proc = subprocess.run(
                ["ps", "-o", "pid=,rss=,time=", "-p", ",".join(map(str, sorted(pids)))],
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
            rows = [line.split() for line in proc.stdout.splitlines() if line.strip()]
            found = {int(row[0]) for row in rows}
            result.update(
                sampled_workers=len(found),
                rss_kb=sum(int(row[1]) for row in rows),
                cpu_lifetime_ms=round(sum(_cpu_seconds(row[2]) for row in rows) * 1000, 1),
                complete=found == pids,
            )
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            pass  # Preserve the explicitly incomplete snapshot when a worker exits.
    return result


def pool_snapshot(store: Any, *, resources: bool = False) -> dict[str, Any]:
    proof = getattr(store, "_proof", None)
    pool = getattr(proof, "_pool", None)
    executors = list(getattr(pool, "_worker_slots", ()))
    default = getattr(pool, "_executor", None)
    if default is not None:
        executors.append(default)
    # Private executor census is best-effort; copy to tolerate process exits.
    live = {
        child.pid
        for executor in executors
        for child in (getattr(executor, "_processes", None) or {}).copy().values()
        if child.pid is not None and child.is_alive()
    }
    result = {
        "pool_initialized": pool is not None,
        "worker_slots": len(executors),
        "active_pool_workers": len(live),
    }
    if resources:
        result["worker_resources"] = worker_resources(live)
    return result


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
            census = pool_snapshot(self.store, resources=True)["worker_resources"]
            if not census["complete"] or census["rss_kb"] is None:
                self.incomplete += 1
                return
            self.samples += 1
            rss = int(census["rss_kb"])
            self.peak_kb = max(self.peak_kb or 0, rss)
            self.max_workers = max(self.max_workers, census["sampled_workers"])
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


def health(store: Any, port: int) -> dict[str, Any]:
    root = store.inputs.root

    def git(*args: str) -> str | None:
        try:
            return subprocess.check_output(
                ["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=3
            ).strip()
        except (OSError, subprocess.SubprocessError):
            return None

    dirty = git("status", "--porcelain", "--untracked-files=no")
    compiled, fastcore_path = fastcore_binary(root)

    return {
        "pid": os.getpid(),
        "port": port,
        "uptime_sec": round(time.monotonic() - _STARTED, 2),
        "started_at": _STARTED_AT,
        "configured_workers": configured_workers(),
        "pool": pool_snapshot(store),
        "fastcore_compiled": compiled,
        "fastcore_path": fastcore_path,
        "git_head": git("rev-parse", "HEAD"),
        "git_dirty": None if dirty is None else bool(dirty),
        "python_version": sys.version.split()[0],
        "locks": {
            name: getattr(store, f"proof_{name}_lock").locked()
            for name in ("search", "profile", "windows")
        },
        "data": {
            "intake": (root / "data/w1/current-intake.json").exists(),
            "lineage": (root / "data/benchmark/lqdt-lineage.json").exists(),
            "h1_seal": (root / "data-lake/h1-seal.enabled.json").exists(),
            "calendar_cache_json_files": calendar_cache_count(root),
        },
        "loadavg_1m": os.getloadavg()[0] if hasattr(os, "getloadavg") else None,
    }


class PhaseClock:
    """Measure intervals between progress stages, not internal sub-phase CPU time."""

    def __init__(self) -> None:
        self.stage = "startup"
        self.last = time.perf_counter()
        self.elapsed: dict[str, float] = {}

    def advance(self, event: dict[str, Any]) -> None:
        stage = str(event.get("stage", "unknown"))[:40]
        if stage != self.stage:
            self.finish()
            self.stage = stage

    def finish(self) -> None:
        now = time.perf_counter()
        self.elapsed[self.stage] = self.elapsed.get(self.stage, 0) + (now - self.last) * 1000
        self.last = now


# Function, optional lock name, action-specific keyword arguments.
_ACTIONS: dict[str, Any] = {
    "run": (research_run, None, {"optimize": False}),
    "optimize": (research_run, "search", {"optimize": True}),
    "plateau": (checked_plateau, "profile", {}),
    "windows": (window_summaries, "windows", {}),
    "catalog": (None, None, {}),
}


def _execute(
    store: Any,
    action: str,
    payload: dict[str, Any],
    settings: Any,
    check: Any,
    progress: Any = None,
) -> Any:
    if action == "catalog":
        return store.proof.catalog(override=settings)
    func, lock_name, extra = _ACTIONS[action]
    selection = payload.copy()
    kwargs = dict(extra, settings_override=settings, check_cancelled=check, on_progress=progress)
    if action == "run":
        kwargs["include_plateau"] = selection.pop("include_plateau", None) is True
    lock = getattr(store, f"proof_{lock_name}_lock") if lock_name else None
    if lock is not None and not lock.acquire(blocking=False):
        raise W0Error(f"{action} is busy")
    try:
        return func(store.proof, selection, **kwargs)
    finally:
        if lock is not None:
            lock.release()


def _summary_fields(key: str, value: dict[str, Any]) -> dict[str, Any]:
    rounding = "displayed_trade_net_rounding_delta_rub"
    first = [rounding] if key == "summary" and rounding in value else []
    keys = chain(first, (k for k in value if k not in first))
    fields = (k for k in keys if isinstance(value[k], (str, int, float, bool, type(None))))
    return {
        str(k)[:40]: value[k][:100] if isinstance(value[k], str) else value[k]
        for k in islice(fields, 12)
    }


def _summary(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"result_type": type(result).__name__}
    scalars = (str, int, float, bool)
    compact = {
        k: result[k]
        for k in ["run_id", "status", "rank_score", "seed_used", "trial_count", "pruned_count"]
        if isinstance(result.get(k), scalars)
    }
    for key in ["selection", "summary", "evaluation", "account_checks", "plateau"]:
        value = result.get(key)
        if isinstance(value, dict):
            compact[key] = _summary_fields(key, value)
    compact.update(
        (f"{k}_count", len(result[k]))
        for k in ["rows", "assets", "trades", "ledger", "series"]
        if isinstance(result.get(k), list)
    )
    bounded: dict[str, Any] = {}
    used = 2  # Braces; preserve default JSON separators and the UTF-8 wire budget.
    for key, value in compact.items():
        value = value[:100] if isinstance(value, str) else value
        cost = len(json.dumps({key: value}, ensure_ascii=False).encode("utf-8")) - 2
        cost += 2 if bounded else 0
        if used + cost <= 1100:
            bounded[key] = value
            used += cost
    return bounded


def _options(request: Any) -> tuple[Any, ...]:
    if not isinstance(request, dict):
        raise W0Error("probe body must be an object")
    errors = (
        "invalid probe action, payload or settings_override",
        "repeat must be 1–10 and warmup 0–3",
        "invalid probe response_mode or profile",
        "sample_worker_rss must be boolean",
    )
    # Defaults define types; bool/int must match exactly (True is not a repeat count).
    rules: tuple[tuple[str, Any, Any, int], ...] = (
        ("action", "", _ACTIONS, 0),
        ("payload", {}, None, 0),
        ("settings_override", {}, None, 0),
        ("repeat", 1, range(1, 11), 1),
        ("warmup", 0, range(4), 1),
        ("profile", False, None, 2),
        ("response_mode", "summary", ("summary", "full"), 2),
        ("sample_worker_rss", False, None, 3),
    )
    values = []
    for name, default, allowed, error in rules:
        value, kind = request.get(name, default), type(default)
        valid = type(value) is kind if kind in (int, bool) else isinstance(value, kind)
        if not valid or (allowed is not None and value not in allowed):
            raise W0Error(errors[error])
        values.append(value)
    return tuple(values)


def _merge_phases(total: dict[str, float], elapsed: dict[str, float]) -> None:
    for key, value in elapsed.items():
        if key in total or len(total) < 8:
            total[key] = round(total.get(key, 0) + value, 2)


def probe(store: Any, request: Any, check: Any) -> dict[str, Any]:
    action, payload, override, repeat, warmup, profile, mode, sample_rss = _options(request)
    settings = settings_with_override(store.inputs.root, override)
    for _ in range(warmup):
        _execute(store, action, payload, settings, check)
    profiler = cProfile.Profile() if profile else None
    durations = []
    phases: dict[str, float] = {}
    cpu_start = time.process_time()
    usage_start = resource.getrusage(resource.RUSAGE_SELF)
    result: Any = None
    with (
        profiler or nullcontext(),
        collect_cache_metrics() as cache_metrics,
        collect_phase_metrics() as phase_metrics,
        collect_worker_metrics() as worker_metrics,
        sample_worker_rss(store, sample_rss) as rss_sampler,
    ):
        for _ in range(repeat):
            started = time.perf_counter()
            clock = PhaseClock()
            result = _execute(store, action, payload, settings, check, clock.advance)
            clock.finish()
            _merge_phases(phases, clock.elapsed)
            durations.append(round((time.perf_counter() - started) * 1000, 2))
    usage = resource.getrusage(resource.RUSAGE_SELF)
    telemetry: dict[str, Any] = {
        "wall_ms": durations,
        "cache_metrics_main_thread": cache_metrics,
        "worker_task_metrics": worker_metrics.snapshot(),
        "pool": pool_snapshot(store, resources=True),
        "progress_phase_ms": phases,
        "operation_phase_ms": {name: round(ms, 2) for name, ms in phase_metrics.items()},
        "min_ms": min(durations),
        "median_ms": statistics.median(durations),
        "p95_ms": sorted(durations)[math.ceil(len(durations) * 0.95) - 1],
        "cpu_user_ms": round((usage.ru_utime - usage_start.ru_utime) * 1000, 2),
        "cpu_sys_ms": round((usage.ru_stime - usage_start.ru_stime) * 1000, 2),
        "max_rss_kb": round(usage.ru_maxrss / 1024)
        if sys.platform == "darwin"
        else usage.ru_maxrss,
        "cpu_process_ms": round((time.process_time() - cpu_start) * 1000, 2),
    }
    if rss_sampler is not None:
        telemetry["worker_rss_sampling"] = rss_sampler.snapshot()
    if profiler:
        telemetry["profile"] = profile_summary(profiler)
    return {
        "action": action,
        "result": result if mode == "full" else _summary(result),
        "telemetry": telemetry,
    }
