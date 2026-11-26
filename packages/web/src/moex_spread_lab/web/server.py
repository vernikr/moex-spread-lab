"""Loopback-only HTTP server, ScreenerStore, and the running-instance discovery record.

`serve` binds the request handler to a `ScreenerStore` and publishes an atomic,
ownership-checked `data/analytics/server-instance.json` record while it runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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


def write_instance(root: Path, port: int) -> str:
    folder = root / "data/analytics"
    folder.mkdir(parents=True, exist_ok=True)
    identity = uuid.uuid4().hex
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    record: dict[str, Any] = {
        "pid": os.getpid(),
        "port": port,
        "started_at": datetime.now(UTC).isoformat(),
        "git_head": result.stdout.strip() if result.returncode == 0 else None,
        "instance_id": identity,
    }
    path = folder / "server-instance.json"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=folder, delete=False
        ) as handle:
            temporary = handle.name
            json.dump(record, handle)
        os.replace(temporary, path)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    return identity


def remove_instance(root: Path, identity: str) -> None:
    path = root / "data/analytics/server-instance.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict) and record.get("instance_id") == identity:
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        # Do not prevent server shutdown if another process changed the file.
        pass


def serve(inputs: _pair_screener.ScreenerInputs, port: int = 8765, host: str = "127.0.0.1") -> None:
    if not 0 <= port <= 65535:
        raise _pair_screener.ScreenerError("порт должен быть в диапазоне 0–65535")
    if host not in {"127.0.0.1", "0.0.0.0"}:
        raise _pair_screener.ScreenerError("host должен быть 127.0.0.1 или 0.0.0.0")
    store = ScreenerStore(inputs)
    server = ThreadingHTTPServer((host, port), make_handler(store))
    identity = None
    try:
        identity = write_instance(inputs.root, server.server_port)
        print(f"Local research screener: http://{host}:{server.server_port}", flush=True)
        server.serve_forever()
    finally:
        server.server_close()
        store.close()
        if identity:
            remove_instance(inputs.root, identity)
