"""Loopback-only HTTP server, ScreenerStore, and static ES-module asset serving."""

from __future__ import annotations

import json
import select
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from moex_spread_lab.core.settings import read_settings, save_settings
from moex_spread_lab.core.timeframes import DEFAULT_TIMEFRAME, get_timeframe, timeframe_options
from moex_spread_lab.core.universe import (
    EXTRA_PAIRS,
    GLD_HISTORICAL_FAR,
    GLD_HISTORICAL_PAIRS,
    HISTORICAL_FAR_BY_NEAR,
    HISTORICAL_PERPETUAL_PAIRS,
    HISTORY_WINDOWS,
    INITIAL_CAPITAL,
    MAX_QUANTITY,
    MIN_DAILY_VOLUME,
    PAIRS,
    PERPETUAL_HISTORY_STARTS,
    PERPETUAL_HISTORY_TILL,
    SCREEN_PAIRS,
    STRATEGIES,
)
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.funding_carry.carry import fetch_history
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    _CANDLE_SUMMARY_CACHE,
    ScreenerError,
    ScreenerInputs,
    _candle_summary,
    _leg_volumes,
    _offline_only,
    _pair,
    _pair_history_span,
    _parameters,
    _positive_leg_volume_dates,
    _session_volumes,
    execution_settings,
    pair_price_inputs,
)
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    _history_source as _pair_history_source,
)
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    build_result as _pair_build_result,
)
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    catalog as _pair_catalog,
)

from .routes_legacy import LEGACY_POST_ROUTES, handle_get_legacy, handle_post_legacy
from .routes_proof import (
    LARGE_PROOF_POST_ROUTES,
    PROOF_POST_ROUTES,
    handle_get_proof,
    handle_post_proof,
    strategy_registry_payload,
)
from .routes_settings import handle_get_settings, handle_post_settings
from .store import ScreenerStore

WEB_PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = (WEB_PACKAGE_DIR / "static").resolve()
VENDOR_DIR = (WEB_PACKAGE_DIR / "vendor").resolve()

_STATIC_MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}

_CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'"
)

_ALL_POST_ROUTES = frozenset({"/api/settings"}) | LEGACY_POST_ROUTES | PROOF_POST_ROUTES

__all__ = [
    "EXTRA_PAIRS",
    "GLD_HISTORICAL_FAR",
    "GLD_HISTORICAL_PAIRS",
    "HISTORICAL_FAR_BY_NEAR",
    "HISTORICAL_PERPETUAL_PAIRS",
    "HISTORY_WINDOWS",
    "INITIAL_CAPITAL",
    "MAX_QUANTITY",
    "MIN_DAILY_VOLUME",
    "PAIRS",
    "PERPETUAL_HISTORY_STARTS",
    "PERPETUAL_HISTORY_TILL",
    "SCREEN_PAIRS",
    "STATIC_DIR",
    "STRATEGIES",
    "VENDOR_DIR",
    "_CANDLE_SUMMARY_CACHE",
    "ScreenerError",
    "ScreenerInputs",
    "ScreenerStore",
    "_candle_summary",
    "_history_source",
    "_leg_volumes",
    "_offline_only",
    "_pair",
    "_pair_history_span",
    "_parameters",
    "_positive_leg_volume_dates",
    "_session_volumes",
    "build_result",
    "catalog",
    "execution_settings",
    "fetch_history",
    "get_timeframe",
    "make_handler",
    "pair_price_inputs",
    "read_settings",
    "save_settings",
    "serve",
    "strategy_registry_payload",
    "timeframe_options",
]


def _history_source(inputs: ScreenerInputs, pair_id: str, secid: str) -> dict[str, Any]:
    return _pair_history_source(inputs, pair_id, secid, fetch_fn=fetch_history)


def catalog(
    inputs: ScreenerInputs,
    *,
    registry: Any = PAIRS,
    funding: Any = None,
    initial_capital: float = INITIAL_CAPITAL,
    timeframe: str = DEFAULT_TIMEFRAME,
) -> dict[str, Any]:
    return _pair_catalog(
        inputs,
        registry=registry,
        funding=funding,
        initial_capital=initial_capital,
        timeframe=timeframe,
        fetch_fn=fetch_history,
    )


def build_result(
    inputs: ScreenerInputs,
    selection: dict[str, Any],
    *,
    through: str | None = None,
    validation_share: float | None = None,
) -> dict[str, Any]:
    return _pair_build_result(
        inputs,
        selection,
        through=through,
        validation_share=validation_share,
        fetch_fn=fetch_history,
    )


def _resolve_static_asset(url_path: str) -> tuple[Path, str] | None:
    rel = unquote(url_path.removeprefix("/static/"))
    if not rel or "\x00" in rel or rel.startswith("/"):
        return None
    try:
        candidate = (STATIC_DIR / rel).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(STATIC_DIR) or not candidate.is_file():
        return None
    mime = _STATIC_MIME_TYPES.get(candidate.suffix)
    if mime is None:
        return None
    return candidate, mime


def make_handler(store: ScreenerStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", _CSP_HEADER)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The browser superseded this run with a newer selection.

        def _drain_body(self, size: int) -> None:
            # Loopback clients otherwise see a connection reset instead of the
            # 400: kernels RST sockets closed with unread received data (most
            # visible on macOS). Drain the declared body first, bounded.
            remaining = max(0, min(size, 64 * 1024 * 1024))
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)

        def _json(self, status: int, value: Any) -> None:
            self._send(
                status,
                json.dumps(value, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _start_ndjson(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", _CSP_HEADER)
            self.close_connection = True
            self.end_headers()

        def _emit_ndjson(self, value: Any) -> None:
            line = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                self.wfile.write(line)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise ConnectionAbortedError("Клиент закрыл соединение.") from exc

        def _local_request(self) -> bool:
            host = self.headers.get("Host", "")
            port = self.server.server_port
            return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _local_origin(self) -> bool:
            return self.headers.get("Origin") in (
                None,
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            )

        def do_GET(self) -> None:
            if not self._local_request() or not self._local_origin():
                self._json(403, {"error": "local host only"})
                return
            path = urlsplit(self.path).path
            if path in {"/", "/proof"}:
                try:
                    self._send(
                        200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8"
                    )
                except OSError:
                    self._json(404, {"error": "not found"})
                return
            if path == "/vendor/lightweight-charts.standalone.production.js":
                vendor = VENDOR_DIR / "lightweight-charts.standalone.production.js"
                try:
                    self._send(200, vendor.read_bytes(), "application/javascript; charset=utf-8")
                except OSError:
                    self._json(404, {"error": "not found"})
                return
            if path.startswith("/static/"):
                resolved = _resolve_static_asset(path)
                if resolved is None:
                    self._json(404, {"error": "not found"})
                    return
                asset_path, mime = resolved
                try:
                    self._send(200, asset_path.read_bytes(), mime)
                except OSError:
                    self._json(404, {"error": "not found"})
                return
            try:
                handled, payload = handle_get_settings(path, store)
                if handled:
                    self._json(200, payload)
                    return
                handled, payload = handle_get_proof(path, store)
                if handled:
                    self._json(200, payload)
                    return
                handled, payload = handle_get_legacy(path, store, catalog_fn=catalog)
                if handled:
                    self._json(200, payload)
                    return
            except (W0Error, ValueError, OSError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(404, {"error": "not found"})

        def _check_connected(self) -> None:
            # Abort expensive group replay after its browser fetch has been cancelled.
            if select.select([self.connection], [], [], 0)[0] and not self.connection.recv(
                1, socket.MSG_PEEK
            ):
                raise ConnectionAbortedError("Клиент отменил подбор.")

        def do_POST(self) -> None:
            if not self._local_request() or not self._local_origin():
                self._json(403, {"error": "local origin only"})
                return
            path = urlsplit(self.path).path
            if path not in _ALL_POST_ROUTES:
                self._json(404, {"error": "not found"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                self._json(415, {"error": "JSON body required"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                limit = 192 * 1024 if path in LARGE_PROOF_POST_ROUTES else 8192
                if not 0 < size <= limit:
                    self._drain_body(size)
                    raise ScreenerError(f"размер запроса должен быть 1–{limit} байт")
                selection = json.loads(self.rfile.read(size))

                handled, result = handle_post_settings(path, selection, store)
                if not handled:
                    handled, already_sent, result = handle_post_proof(
                        path,
                        selection,
                        store,
                        stream=self.headers.get("X-Progress-Stream") == "1",
                        start_ndjson=self._start_ndjson,
                        emit_ndjson=self._emit_ndjson,
                        send_json=self._json,
                        check_connected=self._check_connected,
                    )
                    if handled and already_sent:
                        return
                if not handled:
                    handled, result = handle_post_legacy(
                        path,
                        selection,
                        store,
                        build_result_fn=build_result,
                    )
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                return
            except (ValueError, OSError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, result)

        def log_message(self, _format: str, *_args: Any) -> None:
            pass

    return Handler


def serve(inputs: ScreenerInputs, port: int = 8765) -> None:
    if not 0 <= port <= 65535:
        raise ScreenerError("порт должен быть в диапазоне 0–65535")
    store = ScreenerStore(inputs)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
    print(f"Local research screener: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.close()
