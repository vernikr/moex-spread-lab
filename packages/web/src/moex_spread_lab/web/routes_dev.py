"""Loopback-only, non-persisting diagnostics for the workstation screener.

`health` and `probe` back `/api/dev/*`; their read-only helpers (fastcore/cache census,
profile summary, pool/worker census, RSS sampling) never start a pool or write to disk.
"""

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
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moex_spread_lab.core import _fastcore_loader
from moex_spread_lab.core.cache_metrics import collect_cache_metrics
from moex_spread_lab.core.settings import settings_with_override
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.phase_metrics import collect_phase_metrics
from moex_spread_lab.proof.proof_batch import checked_plateau
from moex_spread_lab.proof.proof_pool import configured_workers
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.proof_windows import window_summaries
from moex_spread_lab.proof.worker_metrics import collect_worker_metrics

_STARTED = time.monotonic()
_STARTED_AT = datetime.now(UTC).isoformat()


def fastcore_binary(root: Path) -> tuple[bool, str | None]:
    """Report the normal binary's presence and privacy-preserving location, not load state."""
    path = (
        Path(_fastcore_loader.__file__)
        .resolve()
        .with_name("_fastcore" + (sysconfig.get_config_var("EXT_SUFFIX") or ".so"))
    )
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
    if not pids:
        return {"sampled_workers": 0, "rss_kb": 0, "cpu_lifetime_ms": 0, "complete": True}
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
        rss = sum(int(row[1]) for row in rows)
        cpu_ms = round(sum(_cpu_seconds(row[2]) for row in rows) * 1000, 1)
        return {
            "sampled_workers": len(found),
            "rss_kb": rss,
            "cpu_lifetime_ms": cpu_ms,
            "complete": found == pids,
        }
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return {"sampled_workers": 0, "rss_kb": None, "cpu_lifetime_ms": None, "complete": False}


def pool_snapshot(store: Any, *, resources: bool = False) -> dict[str, Any]:
    proof = getattr(store, "_proof", None)
    pool = getattr(proof, "_pool", None)
    if pool is None:
        result: dict[str, Any] = {
            "pool_initialized": False,
            "worker_slots": 0,
            "active_pool_workers": 0,
        }
        if resources:
            result["worker_resources"] = worker_resources(set())
        return result
    executors = list(getattr(pool, "_worker_slots", ()))
    default = getattr(pool, "_executor", None)
    if default is not None:
        executors.append(default)
    live: set[int] = set()
    for executor in executors:
        # ProcessPoolExecutor exposes no public process census; this is a
        # best-effort snapshot of its currently registered child processes.
        processes = getattr(executor, "_processes", None) or {}
        for child in processes.copy().values():
            if child.pid is not None and child.is_alive():
                live.add(child.pid)
    result = {
        "pool_initialized": True,
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
            pool = pool_snapshot(self.store, resources=True)
            census = pool["worker_resources"]
            if not census["complete"] or census["rss_kb"] is None:
                self.incomplete += 1
                return
            self.samples += 1
            rss = int(census["rss_kb"])
            self.peak_kb = max(self.peak_kb if self.peak_kb is not None else 0, rss)
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
            proc = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, text=True, check=False, timeout=3
            )
            return proc.stdout.strip() if proc.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
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
            "search": store.proof_search_lock.locked(),
            "profile": store.proof_profile_lock.locked(),
            "windows": store.proof_windows_lock.locked(),
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
    if action == "run":
        selection = dict(payload)
        selection.pop("include_plateau", None)
        return research_run(
            store.proof,
            selection,
            optimize=False,
            include_plateau=payload.get("include_plateau") is True,
            settings_override=settings,
            check_cancelled=check,
            on_progress=progress,
        )
    funcs = {
        "optimize": (store.proof_search_lock, research_run),
        "plateau": (store.proof_profile_lock, checked_plateau),
        "windows": (store.proof_windows_lock, window_summaries),
    }
    lock, func = funcs[action]
    if not lock.acquire(blocking=False):
        raise W0Error(f"{action} is busy")
    try:
        kwargs: dict[str, Any] = {
            "settings_override": settings,
            "check_cancelled": check,
            "on_progress": progress,
        }
        if action == "optimize":
            kwargs["optimize"] = True
        return func(store.proof, dict(payload), **kwargs)
    finally:
        lock.release()


def _prioritize_rounding(key: str, value: dict[str, Any]) -> dict[str, Any]:
    field = "displayed_trade_net_rounding_delta_rub"
    return {field: value[field], **value} if key == "summary" and field in value else value


def _summary(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"result_type": type(result).__name__}
    keys = ("run_id", "status", "rank_score", "seed_used", "trial_count", "pruned_count")
    compact = {
        key: result[key] for key in keys if isinstance(result.get(key), (str, int, float, bool))
    }
    for key in ("selection", "summary", "evaluation", "account_checks", "plateau"):
        value = result.get(key)
        if isinstance(value, dict):
            value = _prioritize_rounding(key, value)
            compact[key] = {
                k: v
                for k, v in value.items()
                if isinstance(v, (str, int, float, bool)) or v is None
            }
    for key in ("rows", "assets", "trades", "ledger", "series"):
        if isinstance(result.get(key), list):
            compact[f"{key}_count"] = len(result[key])
    # Protect the bridge/context budget even for plugin-supplied strings or fields.
    bounded: dict[str, Any] = {}
    for key, value in compact.items():
        if isinstance(value, str):
            value = value[:100]
        elif isinstance(value, dict):
            value = {
                str(k)[:40]: v[:100] if isinstance(v, str) else v
                for k, v in list(value.items())[:12]
            }
        candidate = {**bounded, key: value}
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) <= 1100:
            bounded[key] = value
    return bounded


def _options(request: Any) -> tuple[str, dict[str, Any], Any, int, int, bool, str, bool]:
    if not isinstance(request, dict):
        raise W0Error("probe body must be an object")
    action = request.get("action")
    payload = request.get("payload", {})
    settings = request.get("settings_override", {})
    repeat, warmup = request.get("repeat", 1), request.get("warmup", 0)
    profile, mode = request.get("profile", False), request.get("response_mode", "summary")
    sample_rss = request.get("sample_worker_rss", False)
    if (
        not isinstance(action, str)
        or action not in {"run", "optimize", "plateau", "windows", "catalog"}
        or not isinstance(payload, dict)
        or not isinstance(settings, dict)
    ):
        raise W0Error("invalid probe action, payload or settings_override")
    if (
        type(repeat) is not int
        or not 1 <= repeat <= 10
        or type(warmup) is not int
        or not 0 <= warmup <= 3
    ):
        raise W0Error("repeat must be 1–10 and warmup 0–3")
    if type(profile) is not bool or not isinstance(mode, str) or mode not in {"summary", "full"}:
        raise W0Error("invalid probe response_mode or profile")
    if type(sample_rss) is not bool:
        raise W0Error("sample_worker_rss must be boolean")
    return action, payload, settings, repeat, warmup, profile, mode, sample_rss


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
    if profiler:
        profiler.enable()
    result: Any = None
    try:
        with (
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
    finally:
        if profiler:
            profiler.disable()
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
