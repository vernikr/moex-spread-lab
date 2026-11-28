"""Base HTTP handler shared by every route module.

Owns response framing (headers, `no-store`, CSP), NDJSON progress streaming, the
loopback-only host/origin guard, static asset resolution, and client-cancellation
detection; route modules decide *what* to answer.
"""

from __future__ import annotations

import json
import os
import select
import socket
from contextlib import suppress
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import unquote

WEB_PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = (WEB_PACKAGE_DIR / "static").resolve()
VENDOR_DIR = (WEB_PACKAGE_DIR / "vendor").resolve()

CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'"
)

STATIC_MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}

INDEX_PAGES = frozenset({"/", "/proof"})


def _header_token(value: str | None) -> str:
    return (value or "").strip().lower()


def _allowed_hosts_env() -> set[str]:
    return {
        _header_token(p) for p in os.environ.get("MSL_ALLOWED_HOSTS", "").split(",") if p.strip()
    }


def resolve_static_asset(url_path: str) -> tuple[Path, str] | None:
    """Map `/static/<rel>` to a file inside `static/`; anything else resolves to `None`."""
    rel = unquote(url_path.removeprefix("/static/"))
    if not rel or "\x00" in rel or rel.startswith("/"):
        return None
    try:
        candidate = (STATIC_DIR / rel).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(STATIC_DIR) or not candidate.is_file():
        return None
    mime = STATIC_MIME_TYPES.get(candidate.suffix)
    if mime is None:
        return None
    return candidate, mime


def vendor_asset() -> Path:
    """The vendored charting bundle served outside `/static/`."""
    return VENDOR_DIR / "lightweight-charts.standalone.production.js"


class LoopbackHandler(BaseHTTPRequestHandler):
    """One request-scoped handler: framing helpers plus loopback-only guards."""

    def send_asset(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP_HEADER)
        self.end_headers()
        # The browser may supersede this run with a newer selection mid-write.
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def drain_body(self, size: int) -> None:
        """Consume an unread request body so loopback clients see the status, not a reset.

        Kernels RST sockets closed with unread received data (most visible on macOS).
        """
        remaining = max(0, min(size, 64 * 1024 * 1024))
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def send_json(self, status: int, value: Any) -> None:
        self.send_asset(
            status,
            json.dumps(value, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def start_ndjson(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP_HEADER)
        self.close_connection = True
        self.end_headers()

    def emit_ndjson(self, value: Any) -> None:
        line = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.wfile.write(line)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ConnectionAbortedError("Клиент закрыл соединение.") from exc

    def server_port(self) -> int:
        """The bound loopback port; `BaseServer` does not declare it."""
        return getattr(self.server, "server_port", 0)

    def local_request(self) -> bool:
        """Loopback by default; `MSL_ALLOWED_HOSTS` is an explicit preview/proxy opt-in only.

        The bind address stays `127.0.0.1`; a cloud-agent preview opts in with the exact preview
        hostname (or `*`) and the API keeps rejecting every other Host.
        """
        host = _header_token(self.headers.get("Host"))
        port = self.server_port()
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        extra = _allowed_hosts_env()
        return "*" in extra or host in allowed or host in extra

    def local_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.server_port()
        extra = _allowed_hosts_env()
        if "*" in extra:
            return True
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        allowed |= {f"https://{host}" for host in extra}
        return _header_token(origin) in allowed

    def local_dev_request(self) -> bool:
        """Development endpoints never inherit preview-host opt-ins."""
        port = self.server_port()
        host = _header_token(self.headers.get("Host"))
        origin = self.headers.get("Origin")
        return (
            self.client_address[0] in {"127.0.0.1", "::1"}
            and host in {f"127.0.0.1:{port}", f"localhost:{port}"}
            and (
                origin is None
                or _header_token(origin) in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            )
        )

    def check_connected(self) -> None:
        """Abort an expensive replay after its browser fetch has been cancelled."""
        if select.select([self.connection], [], [], 0)[0] and not self.connection.recv(
            1, socket.MSG_PEEK
        ):
            raise ConnectionAbortedError("Клиент отменил подбор.")

    def serve_static(self, path: str) -> bool:
        """Answer index/vendor/`/static/` GETs; `False` means "not a static path"."""
        if path in INDEX_PAGES:
            target, mime = STATIC_DIR / "index.html", "text/html; charset=utf-8"
        elif path == "/vendor/lightweight-charts.standalone.production.js":
            target, mime = vendor_asset(), "application/javascript; charset=utf-8"
        elif path.startswith("/static/"):
            resolved = resolve_static_asset(path)
            if resolved is None:
                self.send_json(404, {"error": "not found"})
                return True
            target, mime = resolved
        else:
            return False
        try:
            self.send_asset(200, target.read_bytes(), mime)
        except OSError:
            self.send_json(404, {"error": "not found"})
        return True

    def log_message(self, format: str, *args: Any) -> None:
        """Silence per-request stderr logging: the CLI prints its own status lines."""
        del format, args
