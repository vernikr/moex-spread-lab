"""Calendar-spread proof execution and parameter/liquidity validation."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, TypedDict, cast

from moex_spread_lab.accounting.costs import tariff_profile
from moex_spread_lab.core.contracts import InstrumentContext, WindowSnapshot
from moex_spread_lab.core.settings import INITIAL_CAPITAL
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from ..base import StrategyRegistry
from ..calendar_eval import FILLS, account_trades
from .catalog_builder import build_1d_catalog, build_h1_catalog, enrich_proof_window_and_notes
from .contracts import ASSETS, STRATEGIES, positive_spread_volume_rows, zscore_options
from .intents import calendar_intent_trades
from .proof_payload import build_proof_payload


def _prepare_daily_eligibility(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate calendar rows with the eligibility flags the intent verifier rebinds against.

    Hourly windows carry them from their own liquidity screen; daily windows are admitted by
    positive spread volume alone, so every admitted row is eligible for signal, entry and exit.
    """
    prepared: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("signal_eligible") is None:
            item["signal_eligible"] = True
        if item.get("entry_eligible") is None:
            item["entry_eligible"] = True
        if item.get("exit_eligible") is None:
            item["exit_eligible"] = True
        prepared.append(item)
    return prepared


class CalendarMarket(TypedDict):
    """Runtime knobs the snapshot needs beyond the market data itself."""

    capital: float
    slippage: float
    native_order: bool
    assets: dict[str, dict[str, Any]]
    shared_cache: tuple[dict[Any, Any] | None, Any]


def intent_snapshot(
    window: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    asset: str,
    pair: str,
    timeframe: str,
    market: CalendarMarket,
) -> WindowSnapshot:
    """Build the defensive market snapshot a calendar plugin evaluates its intents on."""
    z_cache, z_cache_key = market["shared_cache"]
    return WindowSnapshot(
        universe=CALENDAR_UNIVERSE,
        pair=pair,
        asset=asset,
        timeframe=timeframe,
        rows=tuple(rows),
        near=window.get("near", ""),
        far=window.get("far", ""),
        initial_capital_rub=market["capital"],
        slippage_ticks=market["slippage"],
        native_spread_order=market["native_order"],
        extra={
            "monthly": bool(market["assets"][asset].get("monthly")),
            "convergence": bool(market["assets"][asset].get("convergence")),
            "tradable_every_session": window.get("tradable_every_session", True),
            # Shared with plugin intents to keep cache telemetry and repeated-trial behaviour consistent.
            "z_cache": z_cache,
            "z_cache_key": z_cache_key,
        },
    )


def _build_h1_eligible_rows(
    window: dict[str, Any], fill: str, quantity: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply the hourly liquidity gate to every session row and flag its eligibility."""
    is_open = fill == "open"
    gate = float(window["liquidity_gate"])
    h1_rows: list[dict[str, Any]] = []
    for src in window["rows"]:
        row = dict(src)
        close_ok = bool(row.get("volume_eligible"))
        bar_vol_ok = float(row.get("min_volume") or 0.0) >= quantity
        open_quote_ok = bool(row.get("has_open_quote", True))
        fill_ok = (
            float(row.get("liquidity_cumulative_before", 0.0)) >= gate
            and bar_vol_ok
            and open_quote_ok
            if is_open
            else close_ok and bar_vol_ok
        )
        row["signal_eligible"] = close_ok
        row["entry_eligible"] = bool(fill_ok)
        row["exit_eligible"] = bool(fill_ok)
        h1_rows.append(row)
    if sum(bool(r["signal_eligible"]) for r in h1_rows) < 2:
        raise W0Error("fewer than two causally liquidity-eligible hourly observations")
    return {**window, "rows": h1_rows}, h1_rows


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
    raw_params: Any,
    registry: StrategyRegistry,
) -> dict[str, Any]:
    if not isinstance(raw_params, Mapping):
        raise W0Error("Некорректные параметры плагина.")
    raw_map = cast(Mapping[str, Any], raw_params)
    context = _instrument_context(asset, pair, timeframe)
    plugin = registry.get(strategy)
    if plugin.requires_daily_bars and timeframe != "1d":
        raise W0Error("Плагин поддерживает только дневные данные.")
    schema = plugin.parameter_schema(context)
    names = [spec.name for spec in schema]
    if (
        len(names) != len(set(names))
        or not any(spec.tunable for spec in schema)
        or set(raw_map) - set(names)
    ):
        raise W0Error("Некорректная схема параметров плагина.")
    plugin_params: Any = plugin.validate_parameters(dict(raw_map), context)
    if not isinstance(plugin_params, dict):
        raise W0Error("Некорректные параметры плагина.")
    plugin_params = cast(dict[str, Any], plugin_params)
    if set(plugin_params) != set(names) or any(
        not spec.contains(plugin_params[spec.name]) for spec in schema
    ):
        raise W0Error("Некорректные параметры плагина.")
    return plugin_params


def _prepare_analysis_rows(
    window: dict[str, Any],
    scope: tuple[Any, ...],
    fill: str,
    quantity: int,
    through: str | None,
    cache: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    timeframe = scope[2]
    if timeframe == "1h":
        h1_cache = cache.setdefault("h1", {}) if cache is not None else None
        h1_key = (*scope, through, fill, quantity)
        if h1_cache is not None and h1_key in h1_cache:
            return h1_cache[h1_key]
        res_window, res_rows = _build_h1_eligible_rows(window, fill, quantity)
        prepared = _prepare_daily_eligibility(res_rows)
        if h1_cache is not None:
            h1_cache[h1_key] = (res_window, prepared)
        return res_window, prepared
    elig_cache = cache.setdefault("daily_eligible", {}) if cache is not None else None
    elig_key = (*scope, through)
    if elig_cache is not None and elig_key in elig_cache:
        return window, elig_cache[elig_key]
    analysis_rows = _prepare_daily_eligibility(positive_spread_volume_rows(window["rows"]))
    if len(analysis_rows) < 2:
        raise W0Error("fewer than two positive-volume calendar-spread observations")
    if elig_cache is not None:
        elig_cache[elig_key] = analysis_rows
    return window, analysis_rows


def _resolve_calendar_params(
    asset: str,
    pair: str,
    timeframe: str,
    strategy: str,
    raw_params: Any,
    registry: StrategyRegistry,
) -> tuple[dict[str, Any], float, dict[str, Any]]:
    """Resolve the parameter envelope and the validated plugin map for one proof replay."""
    plugin_params = _validate_plugin_params(asset, pair, timeframe, strategy, raw_params, registry)
    options = {"entry_z": 1.5, "exit_z": 0.5, **zscore_options(asset)}
    for key in ("entry_z", "exit_z", "window"):
        if key in plugin_params:
            options[key] = plugin_params[key]
    if strategy not in STRATEGIES:
        options.update(plugin_params)
    options["window"] = int(options["window"])
    threshold = float(plugin_params.get("threshold_pp", 1.0))
    return options, threshold, plugin_params


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
    registry: StrategyRegistry = req["registry"]
    through: str | None = req.get("through")
    initial_capital: float = req.get("initial_capital", INITIAL_CAPITAL)
    metrics_only: bool = bool(req.get("metrics_only", False))
    sizing_intents: bool = bool(req.get("sizing_intents", False))
    cache: dict[str, Any] | None = req.get("cache")

    fill = next(f for label, _delay, f in FILLS if label == fill_label)
    # Built-in calendar plugins declare the same adapter as external ones, so they join the
    # intent pipeline while still being handed exactly this parameter map: their own schema fills
    # any default (window, entry_z, exit_z, threshold_pp) before the intents are produced.
    options, threshold, plugin_params = _resolve_calendar_params(
        asset, pair, timeframe, strategy, req.get("raw_params", {}), registry
    )
    profile = {**tariff_profile(slippage), "tariff_group": ASSETS[asset]["group"]}
    scope = (asset, pair, timeframe, window.get("first_date"), window.get("last_date"))
    z_cache = cache.setdefault("z", {}) if cache is not None else None
    z_cache_key = (*scope, through, fill, quantity)
    window, analysis_rows = _prepare_analysis_rows(window, scope, fill, quantity, through, cache)
    native_order = bool(window.get("native_spread_order", True))
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
    series_key = (*scope, through, fill, quantity) if timeframe == "1h" else (*scope, through)
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
    if strategy not in STRATEGIES:
        payload["selection"]["parameters"] = plugin_params
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
