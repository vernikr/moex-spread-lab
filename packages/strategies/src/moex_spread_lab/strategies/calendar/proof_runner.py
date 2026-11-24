"""Calendar-spread proof execution and parameter/liquidity validation."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, cast

from moex_spread_lab.accounting.costs import tariff_profile
from moex_spread_lab.core.contracts import InstrumentContext
from moex_spread_lab.core.settings import INITIAL_CAPITAL
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from ..base import StrategyRegistry
from ..calendar_eval import FILLS, account_trades, strategy_runner
from .catalog_builder import build_1d_catalog, build_h1_catalog, enrich_proof_window_and_notes
from .contracts import ASSETS, STRATEGIES
from .contracts import positive_spread_volume_rows as _positive_spread_volume_rows
from .contracts import zscore_options as _zscore_options
from .intent_window import build_h1_eligible_rows as _build_h1_eligible_rows
from .intent_window import intent_snapshot
from .intent_window import prepare_daily_eligibility as _prepare_daily_eligibility
from .intents import calendar_intent_trades
from .proof_payload import build_proof_payload


def _instrument_context(asset: str, pair: str, timeframe: str) -> InstrumentContext:
    return InstrumentContext(
        universe=CALENDAR_UNIVERSE,
        pair=pair,
        asset=asset,
        timeframe=timeframe,
        monthly=bool(ASSETS[asset].get("monthly")),
        convergence=bool(ASSETS[asset].get("convergence")),
    )


def _validate_plugin_params(
    asset: str,
    pair: str,
    timeframe: str,
    strategy: str,
    raw_params: dict[str, Any],
    registry: StrategyRegistry,
) -> dict[str, Any]:
    context = _instrument_context(asset, pair, timeframe)
    plugin = registry.get(strategy)
    if plugin.requires_daily_bars and timeframe != "1d":
        raise W0Error("Плагин поддерживает только дневные данные.")
    schema = plugin.parameter_schema(context)
    names = [spec.name for spec in schema]
    if (
        len(names) != len(set(names))
        or not any(spec.tunable for spec in schema)
        or set(raw_params) - set(names)
    ):
        raise W0Error("Некорректная схема параметров плагина.")
    plugin_params: Any = plugin.validate_parameters(dict(raw_params), context)
    if not isinstance(plugin_params, dict):
        raise W0Error("Некорректные параметры плагина.")
    plugin_params = cast(dict[str, Any], plugin_params)
    if set(plugin_params) != set(names) or any(
        not spec.contains(plugin_params[spec.name]) for spec in schema
    ):
        raise W0Error("Некорректные параметры плагина.")
    return plugin_params


def _validate_proof_params(
    asset: str,
    pair: str,
    timeframe: str,
    strategy: str,
    raw_params: Any,
    generic: bool,
    registry: StrategyRegistry,
) -> tuple[dict[str, Any], float, dict[str, Any] | None]:
    if not isinstance(raw_params, dict):
        raise W0Error("unknown strategy parameter")
    raw_params = cast(dict[str, Any], raw_params)
    options: dict[str, Any] = {"entry_z": 1.5, "exit_z": 0.5, **_zscore_options(asset)}
    if generic:
        plugin_params = _validate_plugin_params(
            asset, pair, timeframe, strategy, raw_params, registry
        )
        options["window"] = int(options["window"])
        return options, 1.0, plugin_params
    if set(raw_params) - {"entry_z", "exit_z", "window", "threshold_pp"}:
        raise W0Error("unknown strategy parameter")
    for key in ("entry_z", "exit_z", "window"):
        if key in raw_params:
            options[key] = raw_params[key]
    threshold = float(raw_params.get("threshold_pp", 1.0))
    if not all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
        for value in (*options.values(), threshold)
    ):
        raise W0Error("strategy parameters must be positive finite numbers")
    options["window"] = int(options["window"])
    return options, threshold, None


def _prepare_analysis_rows(
    window: dict[str, Any],
    timeframe: str,
    fill: str,
    quantity: int,
    through: str | None,
    cache: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if timeframe == "1h":
        h1_cache = cache.setdefault("h1", {}) if cache is not None else None
        h1_key = (through, fill, quantity)
        if h1_cache is not None and h1_key in h1_cache:
            return h1_cache[h1_key]
        res_window, res_rows = _build_h1_eligible_rows(window, fill, quantity)
        prepared = _prepare_daily_eligibility(res_rows)
        if h1_cache is not None:
            h1_cache[h1_key] = (res_window, prepared)
        return res_window, prepared
    elig_cache = cache.setdefault("daily_eligible", {}) if cache is not None else None
    if elig_cache is not None and through in elig_cache:
        return window, elig_cache[through]
    analysis_rows = _prepare_daily_eligibility(_positive_spread_volume_rows(window["rows"]))
    if len(analysis_rows) < 2:
        raise W0Error("fewer than two positive-volume calendar-spread observations")
    if elig_cache is not None:
        elig_cache[through] = analysis_rows
    return window, analysis_rows


def _resolve_calendar_params(
    asset: str,
    pair: str,
    timeframe: str,
    strategy: str,
    raw_params: dict[str, Any],
    generic: bool,
    registry: StrategyRegistry,
) -> tuple[dict[str, Any], float, dict[str, Any] | None]:
    """Resolve the historical envelope and the plugin parameter map for one proof replay."""
    if not generic:
        options, threshold, _ = _validate_proof_params(
            asset, pair, timeframe, strategy, raw_params, False, registry
        )
        return options, threshold, None
    plugin_params = _validate_plugin_params(asset, pair, timeframe, strategy, raw_params, registry)
    if strategy not in STRATEGIES:
        options = {"entry_z": 1.5, "exit_z": 0.5, **_zscore_options(asset), **plugin_params}
        options["window"] = int(options["window"])
        return options, 1.0, plugin_params
    # Built-ins keep their historical envelope; the plugin re-validates exactly that map so the
    # declared parameters (and therefore the payload) stay bit-for-bit identical.
    options, threshold, _ = _validate_proof_params(
        asset, pair, timeframe, strategy, raw_params, False, registry
    )
    plugin = registry.get(strategy)
    context = _instrument_context(asset, pair, timeframe)
    names = tuple(spec.name for spec in plugin.parameter_schema(context))
    return (
        options,
        threshold,
        dict(
            plugin.validate_parameters(
                {name: options[name] for name in names if name in options}, context
            )
        ),
    )


def execute_calendar_proof(
    data: dict[str, Any],
    window: dict[str, Any],
    req: dict[str, Any],
) -> dict[str, Any]:
    asset: str = req["asset"]
    pair: str = req["pair"]
    timeframe: str = req["timeframe"]
    strategy: str = req["strategy"]
    fill_label: str = req["fill_label"]
    quantity: int = req["quantity"]
    slippage: float = req["slippage"]
    generic: bool = req["generic"]
    registry: StrategyRegistry = req["registry"]
    through: str | None = req.get("through")
    initial_capital: float = req.get("initial_capital", INITIAL_CAPITAL)
    metrics_only: bool = bool(req.get("metrics_only", False))
    sizing_intents: bool = bool(req.get("sizing_intents", False))
    cache: dict[str, Any] | None = req.get("cache")

    delay, fill = next((d, f) for label, d, f in FILLS if label == fill_label)
    # ``req["raw_params"]`` is the selection map the historical envelope and the payload used to
    # resolve thresholds from. The built-in calendar plugins declare the same adapter, so they
    # join the intent pipeline while still being handed exactly that map: their own schema fills
    # any default (window, entry_z, exit_z, threshold_pp) before the intents are produced.
    raw_params: dict[str, Any] = cast(dict[str, Any], req["raw_params"])
    options, threshold, plugin_params = _resolve_calendar_params(
        asset, pair, timeframe, strategy, raw_params, generic, registry
    )
    profile = {**tariff_profile(slippage), "tariff_group": ASSETS[asset]["group"]}
    z_cache = cache.setdefault("z", {}) if cache is not None else None
    z_cache_key = (through, fill, quantity)
    window, analysis_rows = _prepare_analysis_rows(
        window, timeframe, fill, quantity, through, cache
    )
    native_order = bool(window.get("native_spread_order", True))
    need_plugin_params = generic and strategy not in STRATEGIES
    if generic:
        assert plugin_params is not None
        snapshot = intent_snapshot(
            window,
            analysis_rows,
            asset=asset,
            pair=pair,
            timeframe=timeframe,
            market={
                "capital": initial_capital,
                "slippage": slippage,
                "native_order": native_order,
                "assets": ASSETS,
                "shared_cache": (z_cache, z_cache_key),
            },
        )
        plugin = registry.get(strategy)
        verdict = plugin.check_compatibility(deepcopy(snapshot))
        if not verdict.compatible:
            raise W0Error(verdict.reason or "Плагин несовместим с выбранным окном.")
        per_pair = {pair: calendar_intent_trades(plugin, snapshot, plugin_params, fill_label)}
    else:
        runner = strategy_runner(
            strategy,
            delay=delay,
            fill=fill,
            close_at_end=True,
            skip_dividend_windows_in_zscore=False,
            threshold=threshold,
            zscore_options=options,
            z_cache=z_cache,
            z_cache_key=z_cache_key,
        )
        per_pair = {pair: runner(analysis_rows)}
    if metrics_only and sizing_intents:
        account: dict[str, Any] = {
            "trade_list": [
                {
                    "direction": item["direction"],
                    "signal": item["signal"],
                    "reason": item["reason"],
                    "entry_row": item["entry"],
                    "exit_row": item["exit"],
                }
                for item in per_pair[pair]
            ]
        }
    else:
        account = account_trades(
            "proof",
            per_pair,
            data["benchmark"],
            profile,
            native_spread=native_order,
            quantity=quantity,
            benchmark_dates=data["benchmark_dates"],
        )
    series_cache = cache.setdefault("series", {}) if (cache is not None and metrics_only) else None
    series_key = (through, fill, quantity) if timeframe == "1h" else (through,)
    payload = build_proof_payload(
        window,
        account,
        {
            "asset": asset,
            "strategy": strategy,
            "fill_label": fill_label,
            "options": options,
            "threshold": threshold,
            "quantity": quantity,
            "slippage": slippage,
            "terms": data["terms"][asset],
            "initial_capital": initial_capital,
        },
        metrics_only=metrics_only,
        sizing_intents=sizing_intents,
        series_cache=series_cache,
        series_key=series_key,
    )
    if need_plugin_params:
        assert plugin_params is not None
        payload["selection"]["parameters"] = cast(dict[str, Any], plugin_params)
    price_source = window.get("price_source", "native_daily_spread")
    payload["selection"].update({"timeframe": timeframe, "price_source": price_source})
    if metrics_only:
        payload["window"].update({"timeframe": timeframe, "price_source": price_source})
        return payload
    enrich_proof_window_and_notes(payload, window, timeframe, price_source)
    return payload


__all__ = [
    "build_1d_catalog",
    "build_h1_catalog",
    "execute_calendar_proof",
]
