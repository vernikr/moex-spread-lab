#!/usr/bin/env python3
"""Run the standalone browser smoke suites offline with a shared browser per worker.

Each worker launches Chromium once; every suite's ``scenario(browser)`` keeps its
own ``BrowserContext`` and loopback server (no shared writable state); fixture
templates are built once per worker and cloned per suite (``_templates``); a
scenario asserting no numerical result may bound its Optuna budget
(``_harness.ui_only_trials``).

Each suite reports its phase split as one ``MOEX_SMOKE_PHASES`` line; the runner
aggregates them into the printed table.
"""

import argparse
import io
import json
import os
import runpy
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SMOKE_DIR = ROOT / "packages/web/tests/browser"
HARNESS_DIR = SMOKE_DIR
PHASES_MARKER = "MOEX_SMOKE_PHASES="
PHASE_FIELDS = (
    "template_build",
    "fixture_clone",
    "server_startup",
    "browser_launch",
    "app_startup",
    "api_compute",
    "static",
    "ui_assertions",
)


def cpu_limit() -> int:
    count = os.cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        count = min(count, len(os.sched_getaffinity(0)))
    return 2 if count >= 4 else 1


@contextmanager
def _offline() -> Iterator[None]:
    """Hold `MOEX_OFFLINE_ONLY=1` for the whole run and restore the caller's value.

    Set once per run rather than per worker: two workers restoring the variable
    concurrently would let an in-flight suite see the previous value.
    """
    previous = os.environ.get("MOEX_OFFLINE_ONLY")
    os.environ["MOEX_OFFLINE_ONLY"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("MOEX_OFFLINE_ONLY", None)
        else:
            os.environ["MOEX_OFFLINE_ONLY"] = previous


def _playwright() -> Any:
    """Import Playwright lazily: it is an optional tool for this runner, not a dependency."""
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _launch(pw: Any) -> Any:
    import _harness

    return _harness.launch(pw)


def _suite_paths(filter_text: str) -> list[Path]:
    return sorted(path for path in SMOKE_DIR.glob("*_smoke.py") if filter_text in path.name)


def _phases_from_output(output: str) -> dict[str, float] | None:
    for line in reversed(output.splitlines()):
        if line.startswith(PHASES_MARKER):
            try:
                parsed = json.loads(line[len(PHASES_MARKER) :])
            except ValueError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def _partition(suites: list[Path], jobs: int) -> list[list[Path]]:
    return [suites[index::jobs] for index in range(jobs)]


def _prepare_imports() -> None:
    for directory in (str(ROOT), str(HARNESS_DIR), str(SMOKE_DIR)):
        if directory not in sys.path:
            sys.path.insert(0, directory)


def _run_scenario(namespace: dict[str, Any], ensure_browser: Callable[[], Any]) -> None:
    scenario = namespace.get("scenario")
    if scenario is None:  # a plain script has already run its module body
        return
    scenario(ensure_browser())
    message = namespace.get("MESSAGE")
    if message:
        print(message)


def _execute(path: Path, ensure_browser: Callable[[], Any]) -> tuple[int, str]:
    captured = io.StringIO()
    code = 0
    with redirect_stdout(captured), redirect_stderr(captured):
        try:
            namespace = runpy.run_path(str(path), run_name=f"moex_smoke_{path.stem}")
            _run_scenario(namespace, ensure_browser)
        except SystemExit as exc:
            # A self-re-executing suite (the plugin catalog) already ran in its child.
            code = int(exc.code or 0)
        except BaseException:  # noqa: BLE001 - one failure must not stop the worker
            code = 1
            traceback.print_exc()
    return code, captured.getvalue()


def _run_suite(path: Path, ensure_browser: Callable[[], Any]) -> dict[str, Any]:
    import _harness

    _harness.reset()
    started = time.monotonic()
    code, output = _execute(path, ensure_browser)
    duration = time.monotonic() - started
    report = _harness.PHASES.report(duration)
    if not {"api_compute", "app_startup"} & report.keys():
        report = _phases_from_output(output) or report
    return {
        "path": path,
        "code": code,
        "duration": duration,
        "output": output,
        "phases": report,
    }


class _WorkerBrowser:
    def __init__(self) -> None:
        self._stack = ExitStack()
        self._browser: Any = None

    def get(self) -> Any:
        if self._browser is None:
            playwright = self._stack.enter_context(_playwright())
            self._browser = _launch(playwright)
            # Registered after Playwright, so the stack closes the browser first.
            self._stack.callback(self._browser.close)
        return self._browser

    def close(self) -> None:
        self._browser = None
        self._stack.close()


def run_worker(paths: list[Path]) -> list[dict[str, Any]]:
    _prepare_imports()
    pool = _WorkerBrowser()
    try:
        return [_run_suite(path, pool.get) for path in paths]
    finally:
        pool.close()


def _print_result(index: int, total: int, result: dict[str, Any]) -> bool:
    name = Path(result["path"]).name
    status = "PASS" if result["code"] == 0 else "FAIL"
    print(
        f"[{index}/{total}] {name} ... {status} ({result['duration']:.1f}s)",
        flush=True,
    )
    if result["code"]:
        print(
            f"--- {name} output (exit {result['code']}) ---\n{result['output'].rstrip()}",
            flush=True,
        )
        return False
    return True


def _print_phases(results: list[dict[str, Any]], elapsed: float) -> None:
    rows = [result for result in results if result["phases"]]
    if not rows:
        return
    header = "suite".ljust(32) + "".join(field[:12].rjust(13) for field in ("total", *PHASE_FIELDS))
    print("\nPhase timings (seconds, per suite and summed):", flush=True)
    print(header, flush=True)
    totals = dict.fromkeys(PHASE_FIELDS, 0.0)
    grand = 0.0
    for result in sorted(rows, key=lambda item: -item["phases"].get("total", 0.0)):
        phases = result["phases"]
        grand += phases.get("total", 0.0)
        for field in PHASE_FIELDS:
            totals[field] += phases.get(field, 0.0)
        cells = "".join(f"{phases.get(field, 0.0):12.1f} " for field in ("total", *PHASE_FIELDS))
        print(Path(result["path"]).name.ljust(32) + cells, flush=True)
    cells = "".join(f"{value:12.1f} " for value in (grand, *(totals[f] for f in PHASE_FIELDS)))
    print("sum".ljust(32) + cells, flush=True)
    print(f"wall {elapsed:.1f}s; phase lines from {len(rows)} suite(s)", flush=True)


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
        help="Concurrent workers (default: CPU-aware)",
    )
    parser.add_argument("--no-timings", action="store_true", help="Skip the aggregated phase table")
    args = parser.parse_args(argv)
    limit = cpu_limit()
    if args.jobs is not None and args.jobs > limit:
        parser.error(f"--jobs {args.jobs} exceeds the CPU-aware limit of {limit}")
    jobs = args.jobs or limit
    suites = _suite_paths(args.filter)
    if not suites:
        print(f"No browser smoke suites match {args.filter!r}", file=sys.stderr)
        return 1
    started = time.monotonic()
    failures = 0
    print(f"Running {len(suites)} browser smoke suite(s), jobs={jobs}, offline=1", flush=True)
    results: list[dict[str, Any]] = []
    with _offline(), ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_worker, group) for group in _partition(suites, jobs)]
        for future in as_completed(futures):
            for result in future.result():
                results.append(result)
                if not _print_result(len(results), len(suites), result):
                    failures += 1
    elapsed = time.monotonic() - started
    if not args.no_timings:
        _print_phases(results, elapsed)
    print(f"Browser smokes: {len(suites) - failures}/{len(suites)} passed in {elapsed:.1f}s")
    return int(failures != 0)


if __name__ == "__main__":
    sys.exit(main())
