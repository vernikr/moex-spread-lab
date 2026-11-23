"""pytest worker sizing for ``scripts/verify.py`` (plan S4a, work item 5).

The A/B on a pinned 2-vCPU runner, measured end-to-end on the whole offline suite
including queue and setup: sequential (``-n 0``) 73.5 s, ``-n 2`` 42.5 s, ``-n 3``
37.5 s, ``-n 4`` 37.0 s. The suite is I/O- and subprocess-heavy, so a third worker
still helps well past the physical core count; the default therefore stays at the
usable CPU budget (never fewer, never oversubscribed by default) and
``MSL_VERIFY_WORKERS`` overrides it for an explicit experiment.

Cgroup-aware: affinity and the cgroup v1/v2 quota both cap the count, because
``os.cpu_count()`` reports host CPUs inside a limited container.
"""

from __future__ import annotations

import os
from pathlib import Path


def cpu_budget() -> int:
    """CPUs this process may actually use: host count, affinity, then cgroup quota."""
    count = os.cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        count = min(count, len(os.sched_getaffinity(0)))
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            count = min(count, max(1, int(quota) // int(period)))
    except (OSError, ValueError):
        pass
    return max(1, count)


def worker_count() -> int:
    """``MSL_VERIFY_WORKERS`` wins when it is a positive integer, else the CPU budget."""
    override = os.environ.get("MSL_VERIFY_WORKERS", "")
    if override.isdigit() and int(override) > 0:
        return int(override)
    return cpu_budget()
