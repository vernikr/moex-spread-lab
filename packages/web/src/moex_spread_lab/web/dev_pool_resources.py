"""Best-effort live worker process resource census (not request deltas)."""

from __future__ import annotations

import subprocess


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
