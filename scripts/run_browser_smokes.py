#!/usr/bin/env python3
"""Run standalone browser smoke suites offline with bounded Chromium concurrency."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SMOKE_DIR = ROOT / "packages/web/tests/browser"


def cpu_limit() -> int:
    """Account for affinity where available, not just the host's CPU count."""
    count = os.cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        count = min(count, len(os.sched_getaffinity(0)))
    return 2 if count >= 4 else 1


def run_suite(path: Path) -> tuple[Path, int, float, str]:
    env = os.environ.copy()
    env["MOEX_OFFLINE_ONLY"] = "1"
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(path)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return path, 1, time.monotonic() - started, str(exc)
    output = result.stdout + result.stderr
    return path, result.returncode, time.monotonic() - started, output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--filter", default="", help="Run suites whose filename contains this substring"
    )
    parser.add_argument(
        "--jobs",
        type=int,
        choices=(1, 2),
        default=None,
        help="Concurrent suites (default: CPU-aware)",
    )
    args = parser.parse_args(argv)
    limit = cpu_limit()
    if args.jobs is not None and args.jobs > limit:
        parser.error(f"--jobs {args.jobs} exceeds the CPU-aware limit of {limit}")
    jobs = args.jobs or limit
    suites = sorted(path for path in SMOKE_DIR.glob("*_smoke.py") if args.filter in path.name)
    if not suites:
        print(f"No browser smoke suites match {args.filter!r}", file=sys.stderr)
        return 1
    started = time.monotonic()
    failures = 0
    print(f"Running {len(suites)} browser smoke suite(s), jobs={jobs}, offline=1", flush=True)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_suite, path) for path in suites]
        for index, future in enumerate(as_completed(futures), 1):
            path, code, duration, output = future.result()
            status = "PASS" if code == 0 else "FAIL"
            print(f"[{index}/{len(suites)}] {path.name} ... {status} ({duration:.1f}s)", flush=True)
            if code:
                failures += 1
                print(f"--- {path.name} output (exit {code}) ---\n{output.rstrip()}", flush=True)
    print(
        f"Browser smokes: {len(suites) - failures}/{len(suites)} passed in {time.monotonic() - started:.1f}s"
    )
    return int(failures != 0)


if __name__ == "__main__":
    sys.exit(main())
