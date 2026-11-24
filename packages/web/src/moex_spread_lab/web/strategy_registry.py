"""Serialize the discovered `StrategyPlugin` registry for the web UI."""

from __future__ import annotations

from typing import Any

from moex_spread_lab.core.contracts import (
    InstrumentContext,
    plugin_is_builtin,
    plugin_ui_field_order,
)
from moex_spread_lab.core.timeframes import DEFAULT_TIMEFRAME
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE, PAIR_UNIVERSE
from moex_spread_lab.proof_runtime.strategy_catalog import (
    get_proof_adapter_ids,
    get_strategy_registry,
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
