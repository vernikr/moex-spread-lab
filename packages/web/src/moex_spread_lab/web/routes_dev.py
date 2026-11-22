"""Loopback-only, non-persisting diagnostics for the workstation screener."""

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
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moex_spread_lab.core import _fastcore_loader
from moex_spread_lab.core.cache_metrics import collect_cache_metrics
from moex_spread_lab.core.settings import settings_with_override
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime.phase_metrics import collect_phase_metrics
from moex_spread_lab.proof_runtime.proof_batch import checked_plateau
from moex_spread_lab.proof_runtime.proof_pool import configured_workers
from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.proof_runtime.proof_windows import window_summaries
from moex_spread_lab.proof_runtime.worker_metrics import collect_worker_metrics

from .dev_pool_metrics import pool_snapshot
from .dev_worker_rss import sample_worker_rss

_STARTED = time.monotonic()
_STARTED_AT = datetime.now(UTC).isoformat()


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

    return {
        "pid": os.getpid(),
        "port": port,
        "uptime_sec": round(time.monotonic() - _STARTED, 2),
        "started_at": _STARTED_AT,
        "configured_workers": configured_workers(),
        "pool": pool_snapshot(store),
        "fastcore_compiled": Path(_fastcore_loader.__file__)
        .with_name("_fastcore" + (sysconfig.get_config_var("EXT_SUFFIX") or ".so"))
        .is_file(),
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
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(10)
        telemetry["profile"] = stream.getvalue()[:3000]
    return {
        "action": action,
        "result": result if mode == "full" else _summary(result),
        "telemetry": telemetry,
    }
