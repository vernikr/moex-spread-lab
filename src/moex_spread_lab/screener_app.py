"""Local-only research screener HTTP server over the analytical engines."""

from __future__ import annotations

import json
import select
import socket
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .carry import fetch_history
from .evaluation_periods import read_settings, save_settings
from .pair_screener import (
    _CANDLE_SUMMARY_CACHE,
    ScreenerError,
    ScreenerInputs,
    _candle_summary,
    _history_source as _pair_history_source,
    _leg_volumes,
    _offline_only,
    _pair,
    _pair_history_span,
    _parameters,
    _positive_leg_volume_dates,
    _session_volumes,
    build_result as _pair_build_result,
    catalog as _pair_catalog,
    execution_settings,
    pair_price_inputs,
)
from .proof import ProofService
from .proof_pool import get_worker_pool
from .timeframes import DEFAULT_TIMEFRAME, get_timeframe, timeframe_options
from .universe import (
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
from .w0 import W0Error

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
    "STRATEGIES",
    "ScreenerError",
    "ScreenerInputs",
    "ScreenerStore",
    "_CANDLE_SUMMARY_CACHE",
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
    "make_handler",
    "pair_price_inputs",
    "serve",
]


def _history_source(inputs: ScreenerInputs, pair_id: str, secid: str) -> dict[str, Any]:
    return _pair_history_source(inputs, pair_id, secid, fetch_fn=fetch_history)


def catalog(
    inputs: ScreenerInputs,
    *,
    registry=PAIRS,
    funding=None,
    initial_capital=INITIAL_CAPITAL,
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


class ScreenerStore:
    def __init__(self, inputs: ScreenerInputs):
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
                calendar=self.calendar,
                pool_factory=get_worker_pool,
            )
        return self._proof

    @property
    def calendar(self) -> Any:
        if self._calendar is None:
            from .calendar_spreads import CalendarData

            self._calendar = CalendarData(self.inputs.root)
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

    def list_runs(self) -> list[dict[str, Any]]:
        return self._recent(self.inputs.history)

    def list_searches(self) -> list[dict[str, Any]]:
        return self._recent(self.inputs.search_history)

    def _save(self, path: Path, result: dict[str, Any]) -> None:
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if any(run["run_id"] == result["run_id"] for run in self._recent(path)):
                return
            record = {**result, "recorded_at": datetime.now(UTC).isoformat()}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                )

    def save(self, result: dict[str, Any]) -> None:
        self._save(self.inputs.history, result)

    def save_screen(self, result: dict[str, Any]) -> None:
        compact = {
            key: result[key] for key in ("run_id", "schema_version", "selection")
        }
        compact["pairs"] = [
            {key: row[key] for key in ("id", "status", "metrics", "evaluation") if key in row}
            for row in result["pairs"]
        ]
        self._save(self.inputs.screen_history, compact)

    def save_search(self, result: dict[str, Any]) -> None:
        self._save(self.inputs.search_history, result)

    def save_proof_search(self, result: dict[str, Any]) -> None:
        self._save(self.inputs.history.parent / "proof-searches.jsonl", result)


def make_handler(store: ScreenerStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'",
            )
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
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'",
            )
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

        def do_GET(self) -> None:
            if not self._local_request():
                self._json(403, {"error": "local host only"})
            elif (path := urlsplit(self.path).path) in {"/", "/proof"}:
                self._send(
                    200,
                    Path(__file__).with_name("proof_ui.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
            elif path == "/vendor/lightweight-charts.standalone.production.js":
                vendor = (
                    Path(__file__).with_name("vendor")
                    / "lightweight-charts.standalone.production.js"
                )
                try:
                    self._send(200, vendor.read_bytes(), "application/javascript; charset=utf-8")
                except OSError:
                    self._json(404, {"error": "not found"})
            elif path == "/api/settings":
                try:
                    self._json(200, read_settings(store.inputs.root))
                except (ValueError, OSError) as exc:
                    self._json(400, {"error": str(exc)})
            elif path == "/api/timeframes":
                self._json(200, timeframe_options())
            elif path == "/api/proof/catalog":
                try:
                    self._json(200, store.proof.catalog())
                except (W0Error, OSError) as exc:
                    self._json(400, {"error": str(exc)})
            elif path == "/api/catalog":
                settings = read_settings(store.inputs.root)
                self._json(
                    200,
                    catalog(
                        store.inputs,
                        initial_capital=settings["initial_capital_rub"],
                        timeframe=settings["timeframe"],
                    ),
                )
            elif path == "/api/runs":
                self._json(200, store.list_runs())
            elif path == "/api/searches":
                self._json(200, store.list_searches())
            else:
                self._json(404, {"error": "not found"})

        def _check_connected(self) -> None:
            # Abort expensive group replay after its browser fetch has been cancelled.
            if select.select([self.connection], [], [], 0)[0] and not self.connection.recv(
                1, socket.MSG_PEEK
            ):
                raise ConnectionAbortedError("Клиент отменил подбор.")

        def do_POST(self) -> None:
            if not self._local_request() or self.headers.get("Origin") not in (
                None,
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            ):
                self._json(403, {"error": "local origin only"})
                return
            path = urlsplit(self.path).path
            if path not in {
                "/api/settings",
                "/api/run",
                "/api/screen",
                "/api/optimize",
                "/api/proof/run",
                "/api/proof/optimize",
                "/api/proof/plateau",
                "/api/proof/windows",
            }:
                self._json(404, {"error": "not found"})
                return
            if (
                self.headers.get("Content-Type", "").split(";", 1)[0]
                != "application/json"
            ):
                self._json(415, {"error": "JSON body required"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                limit = (
                    192 * 1024
                    if path in {"/api/proof/optimize", "/api/proof/plateau", "/api/proof/windows"}
                    else 8192
                )
                if not 0 < size <= limit:
                    self._drain_body(size)
                    raise ScreenerError(f"размер запроса должен быть 1–{limit} байт")
                selection = json.loads(self.rfile.read(size))
                if path == "/api/settings":
                    with store.lock:
                        result = save_settings(store.inputs.root, selection)
                elif path in {
                    "/api/proof/run",
                    "/api/proof/optimize",
                    "/api/proof/plateau",
                    "/api/proof/windows",
                }:
                    if not isinstance(selection, dict):
                        raise ScreenerError("выбор должен быть объектом")
                    stream = self.headers.get("X-Progress-Stream") == "1"
                    search = path == "/api/proof/optimize"
                    from .proof_search import research_run

                    diagnostics = selection.pop("include_plateau", False) is True
                    stream_started = False

                    def run_and_send(fn):
                        nonlocal stream_started
                        if not stream:
                            return fn(None)
                        self._start_ndjson()
                        stream_started = True
                        try:
                            data = fn(lambda ev: self._emit_ndjson({"type": "progress", **ev}))
                            self._emit_ndjson({"type": "result", "data": data})
                        except (ValueError, OSError) as exc:
                            self._emit_ndjson({"type": "error", "error": str(exc)})
                        return None

                    if path in {"/api/proof/windows", "/api/proof/plateau"}:
                        from .proof_batch import checked_plateau
                        from .proof_windows import window_summaries

                        if path == "/api/proof/windows":
                            lock, read = store.proof_windows_lock, window_summaries
                        else:
                            lock, read = store.proof_profile_lock, checked_plateau
                        while not lock.acquire(timeout=0.15):
                            self._check_connected()
                        try:
                            self._check_connected()
                            result = run_and_send(
                                lambda cb: read(
                                    store.proof,
                                    selection,
                                    check_cancelled=self._check_connected,
                                    on_progress=cb,
                                )
                            )
                        finally:
                            lock.release()
                    elif search:
                        if not store.proof_search_lock.acquire(blocking=False):
                            self._json(
                                409,
                                {
                                    "error": "Предыдущий подбор ещё выполняется. Повторите через несколько секунд."
                                },
                            )
                            return
                        try:
                            def _run_search(cb):
                                res = research_run(
                                    store.proof,
                                    selection,
                                    optimize=True,
                                    check_cancelled=self._check_connected,
                                    on_progress=cb,
                                )
                                if res.get("run_id"):
                                    store.save_proof_search(res)
                                return res

                            result = run_and_send(_run_search)
                        finally:
                            store.proof_search_lock.release()
                    else:
                        result = run_and_send(
                            lambda cb: research_run(
                                store.proof,
                                selection,
                                optimize=False,
                                include_plateau=diagnostics,
                                check_cancelled=self._check_connected,
                                on_progress=cb,
                            )
                        )
                    if stream_started:
                        return
                elif path == "/api/run":
                    result = build_result(store.inputs, selection)
                    store.save(result)
                else:
                    from .screener_search import optimize, screen_table

                    active_timeframe = get_timeframe(
                        read_settings(store.inputs.root)["timeframe"]
                    )
                    if active_timeframe.intraday:
                        raise ScreenerError(
                            "Пакетный screener пока работает только на 1d; для выбранного интервала используйте pair-proof."
                        )
                    if path == "/api/screen":
                        result = screen_table(store.inputs, selection)
                        store.save_screen(result)
                    else:
                        result = optimize(store.inputs, selection)
                        store.save_search(result)
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
