"""Loopback-only HTTP server, ScreenerStore, and static ES-module asset serving."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

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

from .dispatch import dispatch_get, dispatch_post, split_path
from .http_handler import LoopbackHandler
from .routes_proof import strategy_registry_payload
from .static_assets import STATIC_DIR, VENDOR_DIR
from .store import ScreenerStore

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


def make_handler(store: ScreenerStore) -> type[BaseHTTPRequestHandler]:
    """Bind one request handler class to the loopback store."""

    class Handler(LoopbackHandler):
        def do_GET(self) -> None:
            if not self.local_request() or not self.local_origin():
                self.send_json(403, {"error": "local host only"})
                return
            dispatch_get(self, store, split_path(self.path), catalog)

        def do_POST(self) -> None:
            if not self.local_request() or not self.local_origin():
                self.send_json(403, {"error": "local origin only"})
                return
            dispatch_post(self, store, split_path(self.path), build_result)

    return Handler


def serve(inputs: ScreenerInputs, port: int = 8765, host: str = "127.0.0.1") -> None:
    if not 0 <= port <= 65535:
        raise ScreenerError("порт должен быть в диапазоне 0–65535")
    if host not in {"127.0.0.1", "0.0.0.0"}:
        raise ScreenerError("host должен быть 127.0.0.1 или 0.0.0.0")
    store = ScreenerStore(inputs)
    server = ThreadingHTTPServer((host, port), make_handler(store))
    print(f"Local research screener: http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.close()
