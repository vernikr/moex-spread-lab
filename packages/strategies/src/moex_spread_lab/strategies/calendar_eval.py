"""Shared calendar spread strategy scans and P&L/benchmark accounting helpers."""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.costs import side_cost
from moex_spread_lab.core.contracts import (
    CompatibilityVerdict,
    EntryBandPoint,
    InstrumentContext,
    ParameterSpec,
    StrategyIntentRun,
    WindowSnapshot,
)
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from .base import (
    CALENDAR_FILLS,
    PRICE_FILLS,
    ZSCORE_CALENDAR_OPTIONS,
    check_calendar_liquidity,
    validate_parameter_map,
)
from .calendar_ruonia.engine import convergence_trades
from .calendar_zscore.engine import zscore_trades

REFERENCE_ROUND_TRIP_COST = 4 * 5.5 + 2 * 2.0
SPREAD_MULTIPLIER = {"secid": "SR", "tariff_group": "stock", "tick_value": 1.0, "tick_size": 1.0}
FILLS = CALENDAR_FILLS
_ACCOUNT_OPTS = frozenset({"native_spread", "quantity", "benchmark_dates"})
_EVAL_OPTS = frozenset(
    {
        "native_spread",
        "reference_cost",
        "skip_dividend_windows_in_zscore",
        "close_at_end",
        "zscore_options",
        "quantity",
    }
)
_RUNNER_OPTS = frozenset(
    {
        "threshold",
        "zscore_options",
        "z_cache",
        "z_cache_key",
    }
)
_BAD_FILL = "Выбранный режим исполнения не поддерживается этой стратегией."
_BAD_UNIVERSE = "Выбранная стратегия неприменима к этому типу пары."


def select_parameters(params: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: params[key] for key in keys if key in params}


def calendar_intent_run(
    strategy_id: str,
    fill: str,
    parameters: dict[str, Any],
    rows: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
) -> StrategyIntentRun:
    last_day = rows[-1]["date"] if rows else None
    intents: tuple[dict[str, Any], ...] = tuple(
        {
            "side": "short_spread" if item["direction"] < 0 else "long_spread",
            "entry_date": item["entry"]["date"],
            "exit_date": item["exit"]["date"],
            "entry_price": item["entry_price"],
            "exit_price": item["exit_price"],
            "signal": item["signal"],
            "reason": item.get("reason") or "end_of_window",
            **({"open_at_end": True} if item["entry"]["date"] == last_day else {}),
        }
        for item in trades
    )
    return StrategyIntentRun(
        strategy_id=strategy_id, fill=fill, parameters=parameters, intents=intents
    )


@dataclass(frozen=True, slots=True)
class CalendarScan:
    window: WindowSnapshot
    rows: list[dict[str, Any]]
    params: dict[str, Any]
    delay: int
    quote: str
    monthly: bool


def calendar_parameters(
    plugin: Any, raw: Mapping[str, Any], context: InstrumentContext | None = None
) -> dict[str, Any]:
    return validate_parameter_map(raw, plugin.parameter_schema(context))


def calendar_compatibility(
    window: WindowSnapshot,
    settings: Any = None,
    *,
    precheck: Callable[[Any], CompatibilityVerdict | None] | None = None,
    series_usable: Callable[[list[dict[str, Any]]], bool] | None = None,
    unavailable_reason: str = "",
) -> CompatibilityVerdict:
    if window.universe != CALENDAR_UNIVERSE:
        return CompatibilityVerdict(False, _BAD_UNIVERSE)
    if precheck is not None:
        verdict = precheck(window)
        if verdict is not None:
            return verdict
    verdict, eligible = check_calendar_liquidity(window, settings)
    if verdict is not None:
        return verdict
    if series_usable is not None and not series_usable(eligible):
        return CompatibilityVerdict(False, unavailable_reason)
    return CompatibilityVerdict(True)


def calendar_evaluate_intents(
    plugin: Any, window: WindowSnapshot, params: Mapping[str, Any], fill: str
) -> StrategyIntentRun:
    if fill not in PRICE_FILLS:
        raise W0Error(_BAD_FILL)
    monthly = bool(plugin.monthly_defaults and window.extra.get("monthly", False))
    ctx = InstrumentContext(
        universe=window.universe,
        pair=window.pair,
        asset=window.asset,
        timeframe=window.timeframe,
        monthly=monthly,
    )
    validated = plugin.validate_parameters(params, ctx)
    verdict = plugin.check_compatibility(window)
    if not verdict.compatible:
        reason = verdict.reason or getattr(
            plugin, "incompatible_reason", "incompatible calendar window"
        )
        raise W0Error(reason)
    delay, quote = PRICE_FILLS[fill]
    rows = (
        list(window.rows)
        if window.timeframe == "1h"
        else [r for r in window.rows if (r.get("min_volume") or 0) > 0]
    )
    state = CalendarScan(window, rows, validated, delay, quote, monthly)
    return calendar_intent_run(
        plugin.strategy_id, fill, validated, window.rows, plugin.scan_trades(state)
    )


def calendar_entry_bands(
    plugin: Any,
    series: list[dict[str, Any]],
    params: Mapping[str, Any],
    fill: str,
    far_point_value_rub: float = 1.0,
) -> list[EntryBandPoint]:
    if fill not in PRICE_FILLS:
        raise W0Error(_BAD_FILL)
    validated = plugin.validate_parameters(select_parameters(params, plugin.band_keys))
    return plugin.band_engine(series, validated, fill, far_point_value_rub=far_point_value_rub)


def trade_costs(
    profile: dict[str, Any] | None, row: dict[str, Any], quantity: int, native_spread: bool
) -> dict[str, float]:
    """Cost of one side at a row's leg values; the reference flat model splits its round trip evenly."""
    if profile is None:
        return {"reference_flat": REFERENCE_ROUND_TRIP_COST / 2 * quantity}
    leg = {**SPREAD_MULTIPLIER, "tariff_group": profile.get("tariff_group", "stock")}
    legs = [
        ({**leg, "secid": "near"}, float(row["near_close"])),
        ({**leg, "secid": "far"}, float(row["far_close"])),
    ]
    return side_cost(profile, legs, quantity, native_spread=native_spread)


def _benchmark_price_at(
    benchmark: dict[str, float], dates: list[str], observation: str | dict[str, Any]
) -> float:
    mark_day = observation.get("benchmark_mark_date") if isinstance(observation, dict) else None
    day = observation.get("date") if isinstance(observation, dict) else observation
    target = str(mark_day or day)[:10]
    index = (
        bisect_right(dates, target) - 1
        if mark_day is not None or "T" not in str(day)
        else bisect_left(dates, target) - 1
    )
    return benchmark[dates[max(0, index)]]


def account_trades(
    label: str,
    per_pair: dict[str, list[dict[str, Any]]],
    benchmark: dict[str, float],
    profile: dict[str, Any] | None,
    **options: Any,
) -> dict[str, Any]:
    unknown = set(options) - _ACCOUNT_OPTS
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {sorted(unknown)}")
    native_spread = bool(options.get("native_spread", False))
    quantity = int(options.get("quantity", 1))
    benchmark_dates: list[str] | None = options.get("benchmark_dates")
    dates = benchmark_dates if benchmark_dates is not None else sorted(benchmark)
    gross = costs = lqdt_on_margin = 0.0
    trades: list[dict[str, Any]] = []
    for pair, items in per_pair.items():
        for item in items:
            entry, exit_row = item["entry"], item["exit"]
            direction = int(item["direction"])
            pnl = direction * (float(item["exit_price"]) - float(item["entry_price"])) * quantity
            entry_cost = trade_costs(profile, entry, quantity, native_spread)
            exit_cost = trade_costs(profile, exit_row, quantity, native_spread)
            cost = sum(entry_cost.values()) + sum(exit_cost.values())
            p_entry = _benchmark_price_at(benchmark, dates, entry)
            p_exit = _benchmark_price_at(benchmark, dates, exit_row)
            hurdle = float(entry["margin"]) * quantity * (p_exit / p_entry - 1)
            gross += pnl
            costs += cost
            lqdt_on_margin += hurdle
            trades.append(
                {
                    "pair": pair,
                    "entry": entry["date"],
                    "exit": exit_row["date"],
                    "direction": direction,
                    "quantity": quantity,
                    "entry_spread": item["entry_price"],
                    "exit_spread": item["exit_price"],
                    "gross_rub": round(pnl, 2),
                    "costs_rub": round(cost, 2),
                    "entry_costs": entry_cost,
                    "exit_costs": exit_cost,
                    "reason": item["reason"],
                    "signal": item["signal"],
                    "margin_rub": round(float(entry["margin"]) * quantity, 2),
                    "lqdt_on_margin_rub": round(hurdle, 2),
                    "entry_row": entry,
                    "exit_row": exit_row,
                }
            )
    net = gross - costs
    return {
        "variant": label,
        "trades": len(trades),
        "winners": sum(t["gross_rub"] - t["costs_rub"] > 0 for t in trades),
        "gross_rub": round(gross, 2),
        "costs_rub": round(costs, 2),
        "net_rub": round(net, 2),
        "lqdt_on_pair_margin_while_open_rub": round(lqdt_on_margin, 2),
        "excess_over_lqdt_on_margin_rub": round(net - lqdt_on_margin, 2),
        "trade_list": trades,
    }


def strategy_runner(
    strategy: str,
    *,
    delay: int,
    fill: str,
    close_at_end: bool,
    skip_dividend_windows_in_zscore: bool,
    **options: Any,
) -> Callable[[list[dict[str, Any]]], list[dict[str, Any]]]:
    unknown = set(options) - _RUNNER_OPTS
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {sorted(unknown)}")
    threshold = float(options.get("threshold", 1.0))
    zscore_options: dict[str, Any] = options.get("zscore_options") or {}
    z_cache: dict[Any, Any] | None = options.get("z_cache")
    z_cache_key: Any = options.get("z_cache_key")
    if strategy == "convergence_to_ruonia":
        return lambda rows: convergence_trades(
            rows,
            threshold=threshold,
            delay=delay,
            fill=fill,
            close_at_end=close_at_end,
            z_cache=z_cache,
            z_cache_key=z_cache_key,
        )
    if strategy == "zscore_mean_reversion":
        return lambda rows: zscore_trades(
            rows,
            delay=delay,
            fill=fill,
            skip_dividend_windows=skip_dividend_windows_in_zscore,
            close_at_end=close_at_end,
            z_cache=z_cache,
            z_cache_key=z_cache_key,
            **zscore_options,
        )
    raise W0Error(f"unknown strategy: {strategy}")


def evaluate_variants(
    pairs: dict[str, list[dict[str, Any]]],
    benchmark: dict[str, float],
    profile: dict[str, Any],
    **options: Any,
) -> list[dict[str, Any]]:
    """Run both strategies under every fill rule on prepared pair rows."""
    unknown = set(options) - _EVAL_OPTS
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {sorted(unknown)}")
    native_spread = bool(options.get("native_spread", False))
    reference_cost = bool(options.get("reference_cost", True))
    skip_div = bool(options.get("skip_dividend_windows_in_zscore", True))
    close_at_end = bool(options.get("close_at_end", False))
    zscore_options: dict[str, Any] | None = options.get("zscore_options")
    quantity = int(options.get("quantity", 1))
    variants: list[dict[str, Any]] = []
    for label, delay, fill in FILLS:
        for strategy in ("convergence_to_ruonia", "zscore_mean_reversion"):
            run = strategy_runner(
                strategy,
                delay=delay,
                fill=fill,
                close_at_end=close_at_end,
                skip_dividend_windows_in_zscore=skip_div,
                zscore_options=zscore_options,
            )
            per_pair = {p: run(rows) for p, rows in pairs.items()}
            if reference_cost:
                variants.append(
                    {
                        "strategy": strategy,
                        "cost_model": "reference_flat_round_trip",
                        **account_trades(label, per_pair, benchmark, None),
                    }
                )
            variants.append(
                {
                    "strategy": strategy,
                    "cost_model": "published_tariff",
                    **account_trades(
                        label,
                        per_pair,
                        benchmark,
                        profile,
                        native_spread=native_spread,
                        quantity=quantity,
                    ),
                }
            )
    return variants


def read_benchmark(path: Path) -> dict[str, float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {row["date"]: float(row["price"]) for row in payload["rows"]}
    except (OSError, ValueError, KeyError) as exc:
        raise W0Error(f"cannot read benchmark: {exc}") from exc


__all__ = [
    "CALENDAR_UNIVERSE",
    "FILLS",
    "REFERENCE_ROUND_TRIP_COST",
    "SPREAD_MULTIPLIER",
    "ZSCORE_CALENDAR_OPTIONS",
    "CalendarScan",
    "CompatibilityVerdict",
    "ParameterSpec",
    "account_trades",
    "calendar_compatibility",
    "calendar_entry_bands",
    "calendar_evaluate_intents",
    "calendar_intent_run",
    "calendar_parameters",
    "convergence_trades",
    "evaluate_variants",
    "read_benchmark",
    "select_parameters",
    "strategy_runner",
    "trade_costs",
    "zscore_trades",
]
