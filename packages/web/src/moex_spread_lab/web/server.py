"""Loopback-only HTTP server and the thread-safe local research store.

`ScreenerStore` caches the shared `ProofService`; `serve` binds the request
handler to one store and answers GET/POST dispatch. Loopback discovery goes
through the `/api/dev/health` endpoint.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from moex_spread_lab.proof.proof_pool import get_worker_pool
from moex_spread_lab.proof.service import ProofService
from moex_spread_lab.strategies import pair_screener as _pair_screener

from .dispatch import dispatch_get, dispatch_post, split_path
from .http_handler import LoopbackHandler


class ScreenerStore:
    def __init__(self, inputs: _pair_screener.ScreenerInputs):
        self.inputs = inputs
        self.lock = threading.Lock()
        self.proof_search_lock = threading.Lock()
        self.proof_profile_lock = threading.Lock()
        self.proof_windows_lock = threading.Lock()
        self._calendar: Any = None
        self._proof: ProofService | None = None

    @property
    def proof(self) -> ProofService:
        if self._proof is None:
            self._proof = ProofService(
                self.inputs.root,
                calendar=self._calendar,
                pool_factory=get_worker_pool,
            )
        return self._proof

    @property
    def calendar(self) -> Any:
        if self._calendar is None:
            self._calendar = self.proof.calendar_data
        return self._calendar

    def close(self) -> None:
        self.proof.close()

    @staticmethod
    def _recent(path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines[-20:] if line]

    def _save(self, path: Path, result: dict[str, Any]) -> None:
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if any(run["run_id"] == result["run_id"] for run in self._recent(path)):
                return
            record = {**result, "recorded_at": datetime.now(UTC).isoformat()}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def save_proof_search(self, result: dict[str, Any]) -> None:
        self._save(self.inputs.history.parent / "proof-searches.jsonl", result)


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
