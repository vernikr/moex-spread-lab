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

from moex_spread_lab.core._fastcore_loader import clean_obj
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
        # `ThreadingHTTPServer` serves first requests concurrently; an unsynchronized lazy
        # `ProofService` would be built twice and the orphaned worker pool never closed.
        self._proof_lock = threading.Lock()

    @property
    def proof(self) -> ProofService:
        with self._proof_lock:
            if self._proof is None:
                self._proof = ProofService(
                    self.inputs.root,
                    calendar=self._calendar,
                    pool_factory=get_worker_pool,
                )
            return self._proof

    @property
    def calendar(self) -> Any:
        with self._proof_lock:
            if self._calendar is None:
                self._calendar = self.proof.calendar_data
            return self._calendar

    def close(self) -> None:
        with self._proof_lock:
            proof = self._proof
            self._proof = None
        if proof is not None:
            proof.close()

    @staticmethod
    def _run_ids(path: Path) -> set[str]:
        """Every recorded ``run_id``; a torn tail must not hide an earlier duplicate.

        Read in binary and decode per line with ``errors="replace"``: an interrupted write can
        leave a half-written UTF-8 sequence, and one such byte must not fail every later save.
        """
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return set()
        ids: set[str] = set()
        for line in raw.splitlines():
            try:
                record = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get("run_id"), str):
                ids.add(record["run_id"])
        return ids

    def _save(self, path: Path, result: dict[str, Any]) -> None:
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if result["run_id"] in self._run_ids(path):
                return
            record = clean_obj({**result, "recorded_at": datetime.now(UTC).isoformat()})
            line = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            prefix = b""
            if path.exists() and path.stat().st_size:
                with path.open("rb") as handle:
                    handle.seek(-1, 2)
                    if handle.read(1) != b"\n":
                        prefix = b"\n"
            with path.open("ab") as handle:
                handle.write(prefix + line.encode("utf-8") + b"\n")

    def save_proof_search(self, result: dict[str, Any]) -> None:
        self._save(self.inputs.history.parent / "proof-searches.jsonl", result)


def make_handler(store: ScreenerStore) -> type[BaseHTTPRequestHandler]:
    """Bind one request handler class to the loopback store."""

    class Handler(LoopbackHandler):
        def do_GET(self) -> None:
            path = split_path(self.path)
            if (
                (path.startswith("/api/dev/") and not self.local_dev_request())
                or not self.local_request()
                or not self.local_origin()
            ):
                self.send_json(403, {"error": "local host only"})
                return
            dispatch_get(self, store, path)

        def do_POST(self) -> None:
            path = split_path(self.path)
            if (
                (path.startswith("/api/dev/") and not self.local_dev_request())
                or not self.local_request()
                or not self.local_origin()
            ):
                self.send_json(403, {"error": "local origin only"})
                return
            dispatch_post(self, store, path)

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
