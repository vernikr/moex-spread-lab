"""HTTP route handlers for execution-proof runs, cohort search and window/plateau reports.

Also serializes the discovered `StrategyPlugin` registry behind `/api/strategies`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from moex_spread_lab.core.contracts import (
    InstrumentContext,
    plugin_is_builtin,
    plugin_ui_field_order,
)
from moex_spread_lab.core.settings import read_settings
from moex_spread_lab.core.timeframes import DEFAULT_TIMEFRAME
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE, PAIR_UNIVERSE
from moex_spread_lab.proof.proof_batch import checked_plateau
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.proof_windows import window_summaries
from moex_spread_lab.proof.strategy_catalog import (
    get_proof_adapter_ids,
    get_strategy_registry,
)
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerError

PROOF_POST_ROUTES = frozenset(
    {
        "/api/proof/run",
        "/api/proof/optimize",
        "/api/proof/plateau",
        "/api/proof/windows",
    }
)
LARGE_PROOF_POST_ROUTES = frozenset(
    {
        "/api/proof/optimize",
        "/api/proof/plateau",
        "/api/proof/windows",
    }
)


def strategy_registry_payload(*, timeframe: str = DEFAULT_TIMEFRAME) -> dict[str, Any]:
    """Serialize discovered ``StrategyPlugin`` schemas, fills, universes, and context specs."""
    registry = get_strategy_registry(refresh=True)
    adapted_ids = get_proof_adapter_ids(registry)
    items: list[dict[str, Any]] = []
    for plugin in registry:
        base_specs = [spec.to_dict() for spec in plugin.parameter_schema()]
        context_params: dict[str, Any] = {}
        if CALENDAR_UNIVERSE in plugin.supported_universes:
            context_params[CALENDAR_UNIVERSE] = {
                tf: {
                    "quarterly": [
                        spec.to_dict()
                        for spec in plugin.parameter_schema(
                            InstrumentContext(
                                universe=CALENDAR_UNIVERSE, timeframe=tf, monthly=False
                            )
                        )
                    ],
                    "monthly": [
                        spec.to_dict()
                        for spec in plugin.parameter_schema(
                            InstrumentContext(
                                universe=CALENDAR_UNIVERSE, timeframe=tf, monthly=True
                            )
                        )
                    ],
                }
                for tf in ("1d", "1h")
            }
        if PAIR_UNIVERSE in plugin.supported_universes:
            context_params[PAIR_UNIVERSE] = {
                tf: {
                    "default": [
                        spec.to_dict()
                        for spec in plugin.parameter_schema(
                            InstrumentContext(universe=PAIR_UNIVERSE, timeframe=tf)
                        )
                    ]
                }
                for tf in ("1d", "1h")
            }
        ui_order = list(plugin_ui_field_order(plugin, tuple(spec["name"] for spec in base_specs)))
        items.append(
            {
                "id": plugin.strategy_id,
                "family_id": plugin.family_id,
                "label": plugin.label_ru,
                "short_label": plugin.short_label_ru,
                "supported_universes": sorted(plugin.supported_universes),
                "supported_fills": list(plugin.supported_fills),
                "requires_daily_bars": bool(plugin.requires_daily_bars),
                "timeframe_supported": timeframe == "1d" or not bool(plugin.requires_daily_bars),
                "proof_adapter": getattr(plugin, "proof_adapter", None),
                "proof_enabled": plugin.strategy_id in adapted_ids,
                "is_builtin": plugin_is_builtin(plugin),
                "ui_order": ui_order,
                "parameters": base_specs,
                "context_parameters": context_params,
            }
        )
    return {
        "schema_version": "strategy-registry.v1",
        "timeframe": timeframe,
        "strategies": items,
    }


def handle_get_proof(path: str, store: Any) -> tuple[bool, Any]:
    """Dispatch GET ``/api/proof/catalog`` and ``/api/strategies``."""
    if path == "/api/proof/catalog":
        return True, store.proof.catalog()
    if path == "/api/strategies":
        try:
            timeframe = read_settings(store.inputs.root)["timeframe"]
            tf = timeframe if isinstance(timeframe, str) else DEFAULT_TIMEFRAME
        except (ValueError, OSError, KeyError):
            tf = DEFAULT_TIMEFRAME
        return True, strategy_registry_payload(timeframe=tf)
    return False, None


def _guarded_run(
    store: Any,
    lock: Any,
    read: Callable[..., Any],
    selection: Any,
    check_connected: Callable[[], None],
    send: Callable[[Callable[[Callable[[dict[str, Any]], None] | None], Any]], Any],
) -> Any:
    """Run one expensive read behind its store lock, aborting on client cancellation."""
    while not lock.acquire(timeout=0.15):
        check_connected()
    try:
        check_connected()
        return send(
            lambda cb: read(
                store.proof,
                selection,
                check_cancelled=check_connected,
                on_progress=cb,
            )
        )
    finally:
        lock.release()


def _search_run(
    store: Any,
    selection: Any,
    check_connected: Callable[[], None],
    on_progress: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    """Optimize a cohort and persist the winner for the picker."""
    result = research_run(
        store.proof,
        selection,
        optimize=True,
        check_cancelled=check_connected,
        on_progress=on_progress,
    )
    if result.get("run_id"):
        store.save_proof_search(result)
    return result


def handle_post_proof(
    path: str,
    selection: Any,
    store: Any,
    *,
    stream: bool,
    start_ndjson: Callable[[], None],
    emit_ndjson: Callable[[Any], None],
    send_json: Callable[[int, Any], None],
    check_connected: Callable[[], None],
) -> tuple[bool, bool, Any]:
    """Dispatch POST ``/api/proof/*`` routes.

    Returns ``(handled, response_already_sent, result_payload)``.
    """
    if path not in PROOF_POST_ROUTES:
        return False, False, None
    if not isinstance(selection, dict):
        raise ScreenerError("выбор должен быть объектом")
    diagnostics = selection.pop("include_plateau", False) is True
    stream_started = False

    def run_and_send(fn: Callable[[Callable[[dict[str, Any]], None] | None], Any]) -> Any:
        nonlocal stream_started
        if not stream:
            return fn(None)
        start_ndjson()
        stream_started = True
        try:
            data = fn(lambda ev: emit_ndjson({"type": "progress", **ev}))
            emit_ndjson({"type": "result", "data": data})
        except (ValueError, OSError) as exc:
            emit_ndjson({"type": "error", "error": str(exc)})
        return None

    if path in {"/api/proof/windows", "/api/proof/plateau"}:
        windows = path == "/api/proof/windows"
        result = _guarded_run(
            store,
            store.proof_windows_lock if windows else store.proof_profile_lock,
            window_summaries if windows else checked_plateau,
            selection,
            check_connected,
            run_and_send,
        )
    elif path == "/api/proof/optimize":
        if not store.proof_search_lock.acquire(blocking=False):
            send_json(
                409,
                {"error": "Предыдущий подбор ещё выполняется. Повторите через несколько секунд."},
            )
            return True, True, None
        try:
            result = run_and_send(lambda cb: _search_run(store, selection, check_connected, cb))
        finally:
            store.proof_search_lock.release()
    else:
        result = run_and_send(
            lambda cb: research_run(
                store.proof,
                selection,
                optimize=False,
                include_plateau=diagnostics,
                check_cancelled=check_connected,
                on_progress=cb,
            )
        )
    return True, stream_started, result
