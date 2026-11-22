"""Best-effort process-pool snapshot for local diagnostics, without starting a pool."""

from __future__ import annotations

from typing import Any

from .dev_pool_resources import worker_resources


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
