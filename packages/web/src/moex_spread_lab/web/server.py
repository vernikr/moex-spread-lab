"""Loopback-only HTTP server and the thread-safe local research store.

`serve` binds the request handler to a `ScreenerStore` and answers GET/POST
dispatch; loopback discovery goes through the `/api/dev/health` endpoint.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from moex_spread_lab.strategies.pair_screener import pair_screener as _pair_screener

from .dispatch import dispatch_get, dispatch_post, split_path
from .http_handler import LoopbackHandler
from .store import ScreenerStore


def make_handler(store: ScreenerStore) -> type[BaseHTTPRequestHandler]:
    """Bind one request handler class to the loopback store."""

    class Handler(LoopbackHandler):
        def do_GET(self) -> None:
            if (
                (self.path.startswith("/api/dev/") and not self.local_dev_request())
                or not self.local_request()
                or not self.local_origin()
            ):
                self.send_json(403, {"error": "local host only"})
                return
            dispatch_get(self, store, split_path(self.path))

        def do_POST(self) -> None:
            if (
                (self.path.startswith("/api/dev/") and not self.local_dev_request())
                or not self.local_request()
                or not self.local_origin()
            ):
                self.send_json(403, {"error": "local origin only"})
                return
            dispatch_post(self, store, split_path(self.path))

    return Handler


def serve(inputs: _pair_screener.ScreenerInputs, port: int = 8765, host: str = "127.0.0.1") -> None:
    if not 0 <= port <= 65535:
        raise _pair_screener.ScreenerError("порт должен быть в диапазоне 0–65535")
    if host not in {"127.0.0.1", "0.0.0.0"}:
        raise _pair_screener.ScreenerError("host должен быть 127.0.0.1 или 0.0.0.0")
    store = ScreenerStore(inputs)
    server = ThreadingHTTPServer((host, port), make_handler(store))
    try:
        print(f"Local research screener: http://{host}:{server.server_port}", flush=True)
        server.serve_forever()
    finally:
        server.server_close()
        store.close()
