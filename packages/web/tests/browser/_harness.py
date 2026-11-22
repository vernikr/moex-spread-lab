"""Shared bootstrap for the standalone Playwright smoke scripts (P0).

Every browser smoke is a plain `python <script>.py` program, not a pytest module, so
`PYTEST_CURRENT_TEST` is unset and `MOEX_OFFLINE_ONLY` used to be missing in most of them. A missing
flag made `CalendarData` fall through to live MOEX ISS history for ~1,050 uncached secids (~180 s per
launch). Importing this module first makes the offline contract explicit and centralises the
boilerplate that previously drifted between eleven scripts:

    from _harness import serve_fixture, temporary_root  # sets MOEX_OFFLINE_ONLY=1 on import

    with sync_playwright() as pw, temporary_root() as root, serve_fixture(root, fixture) as url:
        browser = pw.chromium.launch()
        ...
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

os.environ["MOEX_OFFLINE_ONLY"] = "1"

_REPO_ROOT = Path(__file__).resolve().parents[4]
for _path in (_REPO_ROOT, _REPO_ROOT / "packages" / "proof" / "tests", _REPO_ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

DEFAULT_TIMEOUT_MS = 12_000


@contextmanager
def temporary_root() -> Iterator[Path]:
    """Temporary fixture root; teardown is tolerant of late worker writes."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@contextmanager
def serve(
    root: Path,
    *,
    store_factory: Callable[[Any], Any] | None = None,
) -> Iterator[str]:
    """Serve the loopback screener API on an ephemeral port with a clean shutdown."""
    from moex_spread_lab.web.server import ScreenerInputs, ScreenerStore, make_handler

    store = (store_factory or ScreenerStore)(ScreenerInputs(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        thread.join(timeout=5)


@contextmanager
def serve_fixture(root: Path, builder: Callable[[Path], Any] | None = None) -> Iterator[str]:
    """Build the fixture (if any) and serve it; keeps the temp dir alive for the whole run."""
    if builder is not None:
        builder(root)
    with serve(root) as url:
        yield url


def open_page(
    browser: Any, *, viewport: dict[str, int] | None = None, timeout_ms: int = 0
) -> tuple[Any, list[str]]:
    """New page with the standard viewport/timeouts plus a `pageerror` sink to assert on."""
    page = browser.new_context(viewport=viewport or {"width": 1500, "height": 980}).new_page()
    page.set_default_timeout(timeout_ms or DEFAULT_TIMEOUT_MS)
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    return page, errors
