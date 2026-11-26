"""Offline fixtures, guarded page sessions, streamed API results and phase timings.

The runner shares Chromium per worker; suites own temporary roots and contexts.
Playwright stays optional for the fixture/runner unit tests.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from pathlib import Path
from typing import Any

from _templates import materialize

os.environ["MOEX_OFFLINE_ONLY"] = "1"
_REPO_ROOT = Path(__file__).resolve().parents[4]
for _path in (_REPO_ROOT, _REPO_ROOT / "packages" / "proof" / "tests", _REPO_ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

DEFAULT_TIMEOUT_MS = 20_000
PHASES_MARKER = "MOEX_SMOKE_PHASES="
UI_TRIALS_ENV = "MOEX_UI_SMOKE_TRIALS"
UI_ONLY_TRIALS = 12
PAIR_URL = "/?universe=pair_screen&pair=CNYRUBF%2FCRZ6&strategy=mean_reversion"
CAL_URL = "/proof?universe=calendar_native&asset=SR&pair=SRH3%2FSRM3&strategy=zscore_mean_reversion"
# Startup includes API compute; subtracting both would double-count it.
_MEASURED_PHASES = frozenset({"template_build", "fixture_clone", "server_startup", "api_compute"})
_STARTUP_PAGES: list[Any] = []


class _Phases:
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


PHASES = _Phases()


@contextmanager
def phase(name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        PHASES.add(name, time.perf_counter() - started)


def note_app_startup(page: Any) -> None:
    if not any(known is page for known in _STARTUP_PAGES):
        _STARTUP_PAGES.append(page)
        PHASES.add("app_startup", page.evaluate("performance.now()") / 1000)


def emit_phases(report: dict[str, float]) -> None:
    print(PHASES_MARKER + json.dumps(report, sort_keys=True), flush=True)


def reset() -> None:
    PHASES.reset()
    _STARTUP_PAGES.clear()


@contextmanager
def temporary_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _timed_handler(base: type[BaseHTTPRequestHandler]) -> type[BaseHTTPRequestHandler]:
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


@contextmanager
def serve(root: Path, *, store_factory: Callable[[Any], Any] | None = None) -> Iterator[str]:
    import optuna.logging

    from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs
    from moex_spread_lab.web.server import ScreenerStore, make_handler

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    with phase("server_startup"):
        store = (store_factory or ScreenerStore)(ScreenerInputs(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), _timed_handler(make_handler(store)))
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        thread.join(timeout=5)


def hydrate(root: Path, builder: Callable[[Path], Any], *, template_key: str | None = None) -> None:
    PHASES.extend(materialize(root, builder, template_key))


@contextmanager
def serve_fixture(root: Path, builder: Callable[[Path], Any]) -> Iterator[str]:
    hydrate(root, builder)
    with serve(root) as url:
        yield url


@contextmanager
def ui_only_trials(trials: int = UI_ONLY_TRIALS) -> Iterator[None]:
    """Only UI wiring, never numerical winners, may use the smaller offline budget."""
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
    previous = os.environ.pop(UI_TRIALS_ENV, None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ[UI_TRIALS_ENV] = previous


@contextmanager
def page_session(
    browser: Any, url: str, route: str = PAIR_URL, *, mobile: bool = False
) -> Iterator[Any]:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    context.set_default_timeout(DEFAULT_TIMEOUT_MS)
    errors: list[str] = []
    context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(str(error))))
    context.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    context.on(
        "response",
        lambda r: errors.append(f"HTTP {r.status}: {r.url}") if r.status >= 500 else None,
    )
    try:
        page = context.new_page()
        page.goto(url + route)
        ready(page)
        yield page
        if mobile:
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_function("() => document.documentElement.scrollWidth <= innerWidth")
            open_settings_modal(page)
            page.wait_for_function("() => document.documentElement.scrollWidth <= innerWidth")
            page.keyboard.press("Escape")
        assert not errors, errors
    finally:
        context.close()


@contextmanager
def expect_api_json(page: Any, suffix: str, timeout: int = 120_000) -> Iterator[dict[str, Any]]:
    box: dict[str, Any] = {}
    with page.expect_event(
        "requestfinished", predicate=lambda r: r.url.endswith(suffix), timeout=timeout
    ) as pending:
        yield box
    response = pending.value.response()
    assert response is not None and response.ok, f"API request failed: {suffix}"
    text = response.text()
    if "ndjson" in response.headers.get("content-type", ""):
        messages = [json.loads(line) for line in text.splitlines() if line.strip()]
        assert not [msg for msg in messages if msg["type"] == "error"], messages
        box["data"] = next(msg["data"] for msg in reversed(messages) if msg["type"] == "result")
    else:
        box["data"] = json.loads(text)


def post(page: Any, path: str = "run") -> Any:
    return page.expect_response(
        lambda r: r.url.endswith("/api/proof/" + path), timeout=DEFAULT_TIMEOUT_MS
    )


def expect(value: Any) -> Any:
    # The runner's unit tests import this module without the optional browser dependency.
    return import_module("playwright.sync_api").expect(value)


def ready(page: Any, basis: str | None = None) -> None:
    expect(page.locator("#period-result")).to_be_visible(timeout=DEFAULT_TIMEOUT_MS)
    # RAF polling does not oversleep the UI's 160 ms progress-bar completion timer.
    page.wait_for_function(
        "() => document.getElementById('render-progress').getAttribute('aria-hidden')==='true'"
    )
    expect(page.locator("#ev-runid")).to_have_text(re.compile(r"^[0-9a-f]{16}$"))
    expect(page.locator("#period-result")).to_have_attribute(
        "data-basis", basis or page.evaluate("payload.evaluation.result_basis")
    )
    note_app_startup(page)


def open_settings_modal(page: Any) -> Any:
    page.locator("#open-settings").click()
    dialog = page.locator("#global-settings")
    dialog.wait_for(state="visible")
    return dialog


def apply_settings(page: Any, *, rerun: bool = True, **values: Any) -> dict[str, Any]:
    open_settings_modal(page)
    for name, value in values.items():
        control = page.locator("#" + name.replace("_", "-"))
        if isinstance(value, bool):
            control.set_checked(value)
        elif name == "global_fill":
            control.select_option(value)
        else:
            control.fill(str(value))
    with expect_api_json(page, "/api/settings") as saved:
        if rerun:
            with post(page):
                page.locator("#apply-settings").click()
        else:
            page.locator("#apply-settings").click()
    if rerun:
        ready(page)
    return saved["data"]


def tune(page: Any, button: str = "#auto-tune") -> dict[str, Any]:
    with expect_api_json(page, "/api/proof/optimize") as result:
        page.locator(button).click()
    page.wait_for_function(
        "() => document.getElementById('tune-state').textContent === ''", timeout=120_000
    )
    ready(page)
    return result["data"]


def edit_parameters(page: Any, **values: Any) -> None:
    for name, value in values.items():
        page.locator("#" + name).fill(str(value))
    with post(page):
        page.locator("#" + next(reversed(values))).press("Enter")
    ready(page)


def table_matches_chart(page: Any) -> None:
    expect(page.locator("#window-list [data-reading=pending]")).to_have_count(0)
    row = page.locator("#window-list tr.current")
    expect(row).to_have_attribute("data-reading", "measured")
    expect(row).to_have_attribute("data-run-id", page.evaluate("payload.run_id"))
    expect(row.locator("[data-metric=trades]")).to_have_text(
        str(page.evaluate("payload.summary.trades"))
    )
    expect(row.locator("[data-metric=rate]")).to_have_text(
        page.evaluate(
            "payload.evaluation.result.annualized_excess_pp==null?'—':signed(payload.evaluation.result.annualized_excess_pp)"
        )
    )


def hold_response(page: Any, suffix: str) -> None:
    page.evaluate(
        """suffix => {
      const original = window.fetch;
      let once = true;
      delete window.releaseSmokeResponse;
      window.fetch = async (...args) => {
        const response = await original(...args);
        if (once && String(args[0]).endsWith(suffix)) {
          once = false;
          await new Promise(resolve => window.releaseSmokeResponse = resolve);
        }
        return response;
      };
    }""",
        suffix,
    )


def release_response(page: Any) -> None:
    # Let the released fetch body and its render microtasks run before negative assertions.
    page.evaluate("""async () => {
      window.releaseSmokeResponse();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""")


def launch(pw: Any) -> Any:
    with phase("browser_launch"):
        return pw.chromium.launch()


def standalone(scenario: Callable[[Any], None], message: str = "") -> None:
    from playwright.sync_api import sync_playwright

    reset()
    started = time.perf_counter()
    with sync_playwright() as pw:
        browser = launch(pw)
        try:
            scenario(browser)
        finally:
            browser.close()
    if message:
        print(message)
    emit_phases(PHASES.report(time.perf_counter() - started))
