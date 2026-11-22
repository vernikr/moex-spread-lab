"""Best-effort process-pool snapshot for local diagnostics, without starting a pool."""

from __future__ import annotations

from typing import Any


def pool_snapshot(store: Any) -> dict[str, int | bool]:
    proof = getattr(store, "_proof", None)
    pool = getattr(proof, "_pool", None)
    if pool is None:
        return {"pool_initialized": False, "worker_slots": 0, "active_pool_workers": 0}
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
    return {
        "pool_initialized": True,
        "worker_slots": len(executors),
        "active_pool_workers": len(live),
    }
