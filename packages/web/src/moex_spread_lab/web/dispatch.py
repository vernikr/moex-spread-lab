"""GET/POST dispatch for the loopback server, plus the settings and legacy routes.

`dispatch_get` answers API reads and static assets; `dispatch_post` validates the body
and hands the selection to the settings, proof or legacy screener handlers. The
global-settings/timeframe routes and the legacy S2 pair-screener APIs live here; the
proof routes stay in `routes_proof` and the loopback diagnostics in `routes_dev`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from moex_spread_lab.core.settings import read_settings, save_settings
from moex_spread_lab.core.timeframes import get_timeframe, timeframe_options
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.screener_search import optimize, screen_table
from moex_spread_lab.proof.web_legacy import ScreenerError

from .routes_dev import health, probe
from .routes_proof import (
    LARGE_PROOF_POST_ROUTES,
    PROOF_POST_ROUTES,
    handle_get_proof,
    handle_post_proof,
)

LEGACY_POST_ROUTES = frozenset({"/api/run", "/api/screen", "/api/optimize"})
ALL_POST_ROUTES = (
    frozenset({"/api/settings", "/api/dev/probe"}) | LEGACY_POST_ROUTES | PROOF_POST_ROUTES
)
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


def handle_get_legacy(
    path: str,
    store: Any,
    *,
    catalog_fn: Callable[..., dict[str, Any]],
) -> tuple[bool, Any]:
    """Dispatch GET ``/api/catalog``, ``/api/runs``, and ``/api/searches``."""
    if path == "/api/catalog":
        settings = read_settings(store.inputs.root)
        return True, catalog_fn(
            store.inputs,
            initial_capital=settings["initial_capital_rub"],
            timeframe=settings["timeframe"],
        )
    if path == "/api/runs":
        return True, store.list_runs()
    if path == "/api/searches":
        return True, store.list_searches()
    return False, None


def handle_post_legacy(
    path: str,
    selection: Any,
    store: Any,
    *,
    build_result_fn: Callable[..., dict[str, Any]],
) -> tuple[bool, Any]:
    """Dispatch POST ``/api/run``, ``/api/screen``, and ``/api/optimize``."""
    if path not in LEGACY_POST_ROUTES:
        return False, None
    if path == "/api/run":
        result = build_result_fn(store.inputs, selection)
        store.save(result)
        return True, result
    active_timeframe = get_timeframe(read_settings(store.inputs.root)["timeframe"])
    if active_timeframe.intraday:
        raise ScreenerError(
            "Пакетный screener пока работает только на 1d; для выбранного интервала используйте pair-proof."
        )
    if path == "/api/screen":
        result = screen_table(store.inputs, selection)
        store.save_screen(result)
        return True, result
    result = optimize(store.inputs, selection)
    store.save_search(result)
    return True, result


def dispatch_get(handler: Any, store: Any, path: str, catalog_fn: Any) -> None:
    """Answer one GET: static asset, then settings/proof/legacy API reads."""
    if path == "/api/dev/health":
        handler.send_json(200, health(store, handler.server_port()))
        return
    if handler.serve_static(path):
        return
    try:
        for reader in (
            lambda: handle_get_settings(path, store),
            lambda: handle_get_proof(path, store),
            lambda: handle_get_legacy(path, store, catalog_fn=catalog_fn),
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


def _post_result(handler: Any, store: Any, path: str, selection: Any, build_result: Any):
    """Run the matching route module; returns `(already_sent, result)`."""
    if path == "/api/dev/probe":
        return False, probe(store, selection, handler.check_connected)
    handled, result = handle_post_settings(path, selection, store)
    if handled:
        return False, result
    handled, already_sent, result = handle_post_proof(
        path,
        selection,
        store,
        stream=handler.headers.get("X-Progress-Stream") == "1",
        start_ndjson=handler.start_ndjson,
        emit_ndjson=handler.emit_ndjson,
        send_json=handler.send_json,
        check_connected=handler.check_connected,
    )
    if handled:
        return already_sent, result
    handled, result = handle_post_legacy(path, selection, store, build_result_fn=build_result)
    return False, result


def dispatch_post(handler: Any, store: Any, path: str, build_result: Any) -> None:
    """Answer one POST with validated framing, then the first route that handles it."""
    if path not in ALL_POST_ROUTES:
        handler.send_json(404, {"error": "not found"})
        return
    try:
        selection = _read_selection(handler, path)
        already_sent, result = _post_result(handler, store, path, selection, build_result)
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
