"""Request validation helpers shared by the service facade."""

from typing import Any


def plugin_parameters(
    plugin: Any, request: Any, pair: Any, settings: Any, error: Any, pair_universe: str
) -> dict[str, Any]:
    """Validate trusted-plugin parameters against the schema it declares."""
    from copy import deepcopy

    from moex_spread_lab.core.contracts import InstrumentContext
    from moex_spread_lab.strategies.base import validate_parameter_map

    context = InstrumentContext(
        universe=pair_universe, pair=pair["pair_id"], timeframe=settings.timeframe
    )
    schema = plugin.parameter_schema(context)
    clean = validate_parameter_map(request.get("parameters", {}), schema)
    params = validate_parameter_map(plugin.validate_parameters(deepcopy(clean), context), schema)
    if params != clean or any(not param.contains(params[param.name]) for param in schema):
        raise error("Параметры carry-плагина не соответствуют объявленной схеме.")
    return params


def intent_request(request: Any, registry: Any, settings: Any) -> dict[str, Any]:
    strategy = request.get("strategy", "zscore_mean_reversion")
    selected_plugin = (
        registry.get(strategy) if isinstance(strategy, str) and strategy in registry else None
    )
    default_fill = (
        "same_day_settlement"
        if selected_plugin is not None
        and selected_plugin.supported_fills == ("same_day_settlement",)
        else settings.price_fill
    )
    return {
        **request,
        "quantity": 1,
        "slippage_ticks": settings.slippage_ticks,
        **({} if "fill" in request else {"fill": default_fill}),
    }
