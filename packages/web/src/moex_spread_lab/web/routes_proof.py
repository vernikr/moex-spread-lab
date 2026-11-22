"""HTTP route handlers for execution-proof runs, cohort search, and StrategyPlugin registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from moex_spread_lab.core.contracts import InstrumentContext
from moex_spread_lab.core.settings import read_settings
from moex_spread_lab.core.timeframes import DEFAULT_TIMEFRAME
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE, PAIR_UNIVERSE
from moex_spread_lab.proof_runtime.proof_batch import checked_plateau
from moex_spread_lab.proof_runtime.proof_kernel import PROOF_ADAPTER_IDS
from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.proof_runtime.proof_windows import window_summaries
from moex_spread_lab.strategies import discover_strategies
from moex_spread_lab.strategies.base import (
    calendar_proof_plugin_ids,
    carry_proof_plugin_ids,
    daily_pair_proof_plugin_ids,
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

_BUILTIN_STRATEGY_IDS = frozenset(
    {
        "mean_reversion",
        "funding_only",
        "zscore_mean_reversion",
        "convergence_to_ruonia",
    }
)

_UI_FIELD_ORDER: dict[str, tuple[str, ...]] = {
    "mean_reversion": ("entry_z", "exit_z", "lookback"),
    "zscore_mean_reversion": ("entry_z", "exit_z", "window"),
}


def strategy_registry_payload(*, timeframe: str = DEFAULT_TIMEFRAME) -> dict[str, Any]:
    """Serialize discovered ``StrategyPlugin`` schemas, fills, universes, and context specs."""
    registry = discover_strategies()
    adapted_ids = (
        set(PROOF_ADAPTER_IDS)
        | calendar_proof_plugin_ids(registry)
        | daily_pair_proof_plugin_ids(registry)
        | carry_proof_plugin_ids(registry)
    )
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
        ui_order = list(
            _UI_FIELD_ORDER.get(
                plugin.strategy_id,
                tuple(spec["name"] for spec in base_specs),
            )
        )
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
                "is_builtin": plugin.strategy_id in _BUILTIN_STRATEGY_IDS,
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
            tf = read_settings(store.inputs.root)["timeframe"]
        except (ValueError, OSError, KeyError):
            tf = DEFAULT_TIMEFRAME
        return True, strategy_registry_payload(timeframe=tf)
    return False, None


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

    search = path == "/api/proof/optimize"
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
        if path == "/api/proof/windows":
            lock, read = store.proof_windows_lock, window_summaries
        else:
            lock, read = store.proof_profile_lock, checked_plateau
        while not lock.acquire(timeout=0.15):
            check_connected()
        try:
            check_connected()
            result = run_and_send(
                lambda cb: read(
                    store.proof,
                    selection,
                    check_cancelled=check_connected,
                    on_progress=cb,
                )
            )
        finally:
            lock.release()
    elif search:
        if not store.proof_search_lock.acquire(blocking=False):
            send_json(
                409,
                {"error": "Предыдущий подбор ещё выполняется. Повторите через несколько секунд."},
            )
            return True, True, None
        try:

            def _run_search(cb: Callable[[dict[str, Any]], None] | None) -> dict[str, Any]:
                res = research_run(
                    store.proof,
                    selection,
                    optimize=True,
                    check_cancelled=check_connected,
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
                check_cancelled=check_connected,
                on_progress=cb,
            )
        )
    return True, stream_started, result
