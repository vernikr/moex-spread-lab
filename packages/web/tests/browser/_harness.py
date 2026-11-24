"""Shared bootstrap, phase timers and scenario isolation for the browser smokes (S4b).

Every browser smoke is a plain `python <script>.py` program, not a pytest module, so
`PYTEST_CURRENT_TEST` is unset and `MOEX_OFFLINE_ONLY` used to be missing in most of them. A missing
flag made `CalendarData` fall through to live MOEX ISS history for ~1,050 uncached secids (~180 s per
launch). Importing this module first makes the offline contract explicit and centralises the
boilerplate that previously drifted between eleven scripts:

    from _harness import serve_fixture, temporary_root, scenario_isolation

    def scenario(browser):
        with temporary_root() as root, serve_fixture(root, fixture) as url:
            with scenario_isolation(browser):
                run(browser, url)

    if __name__ == "__main__":
        from _harness import standalone
        standalone(scenario, MESSAGE)

`scenario(browser)` is the callable entry point: `scripts/run_browser_smokes.py` launches
Chromium once per worker and calls it per suite with isolated contexts, while
`standalone()` keeps `python <suite>_smoke.py` working for one-off runs.

Phase timers (`PHASES`) separate the terms the plan asks about — fixture hydration,
server start, browser launch, app startup, API compute — from UI assertions, and print
one `MOEX_SMOKE_PHASES={...}` line per scenario so the runner can aggregate them.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from _templates import materialize

os.environ["MOEX_OFFLINE_ONLY"] = "1"

_REPO_ROOT = Path(__file__).resolve().parents[4]
for _path in (_REPO_ROOT, _REPO_ROOT / "packages" / "proof" / "tests", _REPO_ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

DEFAULT_TIMEOUT_MS = 12_000
PHASES_MARKER = "MOEX_SMOKE_PHASES="
UI_TRIALS_ENV = "MOEX_UI_SMOKE_TRIALS"
#: Optuna budget for scenarios that assert no numerical result (plan S4b, work 4).
UI_ONLY_TRIALS = 12


class _Phases:
    """Thread-safe phase accumulators for one scenario."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: dict[str, float] = {}

    def add(self, name: str, seconds: float) -> None:
        with self._lock:
            self._spans[name] = self._spans.get(name, 0.0) + max(seconds, 0.0)

    def extend(self, spans: dict[str, float]) -> None:
        for name, seconds in spans.items():
            self.add(name, seconds)

    def reset(self) -> None:
        with self._lock:
            self._spans.clear()

    def report(self, total: float) -> dict[str, float]:
        with self._lock:
            spans = dict(self._spans)
        measured = sum(value for name, value in spans.items() if name in _MEASURED_PHASES)
        report = {"total": round(total, 3)}
        report.update({name: round(value, 3) for name, value in sorted(spans.items())})
        report["ui_assertions"] = round(max(total - measured, 0.0), 3)
        return report


#: Spans that are subtracted from the scenario total to estimate UI time. `static` and
#: `app_startup` stay out of the sum because a page's startup already includes its own
#: API compute, so adding both would double-count.
_MEASURED_PHASES = frozenset({"template_build", "fixture_clone", "server_startup", "api_compute"})

PHASES = _Phases()


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Attribute the enclosed block to one phase, even when it raises."""
    started = time.perf_counter()
    try:
        yield
    finally:
        PHASES.add(name, time.perf_counter() - started)


_STARTUP_PAGES: list[Any] = []


def note_app_startup(page: Any) -> None:
    """Count a page's first payload-ready wait once, as the app startup term.

    `performance.now()` is measured from navigation start, so the value covers static
    delivery, script boot and the first proof computation the page waited for.
    """
    if any(known is page for known in _STARTUP_PAGES):
        return
    _STARTUP_PAGES.append(page)
    try:
        milliseconds = page.evaluate("performance.now()")
    except Exception:
        return
    if isinstance(milliseconds, int | float):
        PHASES.add("app_startup", milliseconds / 1000)


def emit_phases(report: dict[str, float]) -> None:
    """Print one machine-readable phase line that the runner parses."""
    print(PHASES_MARKER + json.dumps(report, sort_keys=True), flush=True)


def reset() -> None:
    """Clear per-scenario accumulators; called by every entry point."""
    PHASES.reset()
    _STARTUP_PAGES.clear()


@contextmanager
def temporary_root() -> Iterator[Path]:
    """Temporary fixture root; teardown is tolerant of late worker writes."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _timed_handler(base: type[BaseHTTPRequestHandler]) -> type[BaseHTTPRequestHandler]:
    """Wrap the loopback handler so API compute is measured where it happens."""

    def timed(handler: BaseHTTPRequestHandler, action: Callable[[], None]) -> None:
        started = time.perf_counter()
        try:
            action()
        finally:
            name = "api_compute" if handler.path.startswith("/api/") else "static"
            PHASES.add(name, time.perf_counter() - started)

    def do_get(handler: BaseHTTPRequestHandler) -> None:
        timed(handler, lambda: base.do_GET(handler))  # type: ignore[attr-defined]

    def do_post(handler: BaseHTTPRequestHandler) -> None:
        timed(handler, lambda: base.do_POST(handler))  # type: ignore[attr-defined]

    return type("TimedSmokeHandler", (base,), {"do_GET": do_get, "do_POST": do_post})


def _quiet_optuna() -> None:
    """Silence Optuna's one-INFO-line-per-study chatter in the smoke output.

    Called after the web package imported Optuna, because `optuna.logging` resets the
    level when it is configured at import time.
    """
    try:
        import optuna.logging
    except ImportError:  # the smokes run without Optuna only if the app itself cannot
        return
    optuna.logging.set_verbosity(optuna.logging.WARNING)


@contextmanager
def serve(
    root: Path,
    *,
    store_factory: Callable[[Any], Any] | None = None,
) -> Iterator[str]:
    """Serve the loopback screener API on an ephemeral port with a clean shutdown."""
    from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs
    from moex_spread_lab.web.server import ScreenerStore, make_handler

    _quiet_optuna()
    with phase("server_startup"):
        store = (store_factory or ScreenerStore)(ScreenerInputs(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), _timed_handler(make_handler(store)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        thread.join(timeout=5)


def hydrate(root: Path, builder: Callable[[Path], Any], *, template_key: str | None = None) -> None:
    """Fill ``root`` from a cached fixture template, charging the copy to the phase budget."""
    PHASES.extend(materialize(root, builder, template_key))


@contextmanager
def serve_fixture(
    root: Path,
    builder: Callable[[Path], Any] | None = None,
    *,
    template_key: str | None = None,
) -> Iterator[str]:
    """Hydrate ``root`` from a cached template (if any) and serve it for the whole run."""
    if builder is not None:
        hydrate(root, builder, template_key=template_key)
    with serve(root) as url:
        yield url


@contextmanager
def scenario_isolation(browser: Any) -> Iterator[None]:
    """Close every context a scenario created, so nothing leaks into the next suite.

    Chromium is launched once per worker; a scenario that fails half-way would
    otherwise leave pages alive against its already-closed server. The patch is scoped
    to the `with` block and restored afterwards.
    """
    created: list[Any] = []
    original_context, original_page = browser.new_context, browser.new_page

    def new_context(**kwargs: Any) -> Any:
        context = original_context(**kwargs)
        created.append(context)
        return context

    def new_page(**kwargs: Any) -> Any:
        context = original_context(**kwargs)
        created.append(context)
        return context.new_page()

    browser.new_context, browser.new_page = new_context, new_page
    try:
        yield
    finally:
        browser.new_context, browser.new_page = original_context, original_page
        for context in reversed(created):
            context.close()


@contextmanager
def ui_only_trials(trials: int = UI_ONLY_TRIALS) -> Iterator[None]:
    """Bound the Optuna budget for a scenario that asserts no numerical result.

    The bound only takes effect under the offline contract and only downwards; see
    `moex_spread_lab.proof_runtime.proof_strategy_search.trials_per_strategy`.
    """
    previous = os.environ.get(UI_TRIALS_ENV)
    os.environ[UI_TRIALS_ENV] = str(trials)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(UI_TRIALS_ENV, None)
        else:
            os.environ[UI_TRIALS_ENV] = previous


@contextmanager
def exact_trials() -> Iterator[None]:
    """Explicit opt-out: this scenario asserts an exact trial count or search outcome."""
    previous = os.environ.pop(UI_TRIALS_ENV, None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ[UI_TRIALS_ENV] = previous


def launch(pw: Any) -> Any:
    """Launch Chromium once and charge the cold start to the browser-launch phase."""
    with phase("browser_launch"):
        return pw.chromium.launch()


def standalone(scenario: Callable[[Any], None], message: str = "") -> None:
    """Run one suite directly (`python <suite>_smoke.py`) with a private browser."""
    from playwright.sync_api import sync_playwright

    reset()
    started = time.perf_counter()
    with sync_playwright() as pw:
        browser = launch(pw)
        try:
            scenario(browser)
        finally:
            browser.close()
    report = PHASES.report(time.perf_counter() - started)
    if message:
        print(message)
    emit_phases(report)
