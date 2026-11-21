"""HTTP route handlers for legacy pair-screener catalog, run, screen, and optimize APIs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from moex_spread_lab.core.settings import read_settings
from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.proof_runtime.screener_search import optimize, screen_table
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerError

LEGACY_POST_ROUTES = frozenset({"/api/run", "/api/screen", "/api/optimize"})


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
