"""GET/POST dispatch for the loopback server and the settings/timeframe routes.

`dispatch_get` answers API reads and static assets; `dispatch_post` validates the body
and hands the selection to the settings or proof handlers. The global-settings and
timeframe routes live here; the proof routes stay in `routes_proof` and the loopback
diagnostics in `routes_dev`.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from moex_spread_lab.core.settings import read_settings, save_settings
from moex_spread_lab.core.timeframes import timeframe_options
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerError

from .routes_dev import health, probe
from .routes_proof import (
    LARGE_PROOF_POST_ROUTES,
    PROOF_POST_ROUTES,
    handle_get_proof,
    handle_post_proof,
)

ALL_POST_ROUTES = frozenset({"/api/settings", "/api/dev/probe"}) | PROOF_POST_ROUTES
_MAX_BODY = 192 * 1024
_SMALL_BODY = 8192


def handle_get_settings(path: str, store: Any) -> tuple[bool, Any]:
    """Dispatch GET settings/timeframes routes. Returns ``(handled, payload)``."""
    if path == "/api/settings":
        return True, read_settings(store.inputs.root)
    if path == "/api/timeframes":
        return True, timeframe_options()
    return False, None


def handle_post_settings(path: str, selection: Any, store: Any) -> tuple[bool, Any]:
    """Dispatch POST ``/api/settings``. Returns ``(handled, payload)``."""
    if path == "/api/settings":
        with store.lock:
            return True, save_settings(store.inputs.root, selection)
    return False, None


def dispatch_get(handler: Any, store: Any, path: str) -> None:
    """Answer one GET: static asset, then the settings and proof API reads."""
    if path == "/api/dev/health":
        handler.send_json(200, health(store, handler.server_port()))
        return
    if handler.serve_static(path):
        return
    try:
        for reader in (
            lambda: handle_get_settings(path, store),
            lambda: handle_get_proof(path, store),
        ):
            handled, payload = reader()
            if handled:
                handler.send_json(200, payload)
                return
    except (W0Error, ValueError, OSError) as exc:
        handler.send_json(400, {"error": str(exc)})
        return
    handler.send_json(404, {"error": "not found"})


def _read_selection(handler: Any, path: str) -> Any:
    if handler.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
        raise _UnsupportedMediaType
    size = int(handler.headers.get("Content-Length", "0"))
    limit = (
        _MAX_BODY if path in LARGE_PROOF_POST_ROUTES or path == "/api/dev/probe" else _SMALL_BODY
    )
    if not 0 < size <= limit:
        handler.drain_body(size)
        raise ScreenerError(f"размер запроса должен быть 1–{limit} байт")
    return json.loads(handler.rfile.read(size))


class _UnsupportedMediaType(Exception):
    """Raised when the POST body is not declared as JSON."""


def _post_result(handler: Any, store: Any, path: str, selection: Any):
    """Run the matching route module; returns `(already_sent, result)`."""
    if path == "/api/dev/probe":
        return False, probe(store, selection, handler.check_connected)
    handled, result = handle_post_settings(path, selection, store)
    if handled:
        return False, result
    handled, already_sent, result = handle_post_proof(path, selection, store, handler)
    return already_sent, result


def dispatch_post(handler: Any, store: Any, path: str) -> None:
    """Answer one POST with validated framing, then the first route that handles it."""
    if path not in ALL_POST_ROUTES:
        handler.send_json(404, {"error": "not found"})
        return
    try:
        selection = _read_selection(handler, path)
        already_sent, result = _post_result(handler, store, path, selection)
    except _UnsupportedMediaType:
        handler.send_json(415, {"error": "JSON body required"})
        return
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        return
    except (ValueError, OSError) as exc:
        handler.send_json(400, {"error": str(exc)})
        return
    if not already_sent:
        handler.send_json(200, result)


def split_path(raw_path: str) -> str:
    return urlsplit(raw_path).path
