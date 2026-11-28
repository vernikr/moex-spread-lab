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
    calendar_price,
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
_RUNNER_OPTS = frozenset({"threshold", "zscore_options", "z_cache", "z_cache_key"})
_BAD_FILL = "Выбранный режим исполнения не поддерживается этой стратегией."
_BAD_UNIVERSE = "Выбранная стратегия неприменима к этому типу пары."
_INTENT_KEYS = "side entry_date exit_date entry_price exit_price signal reason"
_TRADE_KEYS = "pair entry exit direction quantity entry_spread exit_spread gross_rub costs_rub entry_costs exit_costs reason signal margin_rub lqdt_on_margin_rub entry_row exit_row"
_ACCOUNT_KEYS = "variant trades winners gross_rub costs_rub net_rub lqdt_on_pair_margin_while_open_rub excess_over_lqdt_on_margin_rub trade_list"


def row_dict(keys: str, values: Sequence[Any]) -> dict[str, Any]:
    return dict(zip(keys.split(), values, strict=True))


def select_parameters(params: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {a: params[a] for a in keys if a in params}


def _calendar_intent(item: Mapping[str, Any], last_day: Any) -> dict[str, Any]:
    a = row_dict(
        _INTENT_KEYS,
        (
            "short_spread" if item["direction"] < 0 else "long_spread",
            item["entry"]["date"],
            item["exit"]["date"],
            item["entry_price"],
            item["exit_price"],
            item["signal"],
            item.get("reason") or "end_of_window",
        ),
    )
    if item["entry"]["date"] == last_day:
        a["open_at_end"] = True
    return a


def calendar_intent_run(
    strategy_id: str,
    fill: str,
    parameters: dict[str, Any],
    rows: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
) -> StrategyIntentRun:
    last_day = rows[-1]["date"] if rows else None
    a = tuple(_calendar_intent(item, last_day) for item in trades)
    return StrategyIntentRun(strategy_id=strategy_id, fill=fill, parameters=parameters, intents=a)


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
        a = precheck(window)
        if a is not None:
            return a
    a, b = check_calendar_liquidity(window, settings)
    if a is not None:
        return a
    if series_usable is not None and not series_usable(b):
        return CompatibilityVerdict(False, unavailable_reason)
    return CompatibilityVerdict(True)


def _rows_for_fill(rows: Sequence[Mapping[str, Any]], fill: str) -> list[dict[str, Any]]:
    if fill != "open":
        return [dict(r) for r in rows]
    out: list[dict[str, Any]] = []
    for src in rows:
        row = dict(src)
        if row.get("has_open_quote") is False:
            row["entry_eligible"] = False
            row["exit_eligible"] = False
        out.append(row)
    return out


def calendar_evaluate_intents(
    plugin: Any, window: WindowSnapshot, params: Mapping[str, Any], fill: str
) -> StrategyIntentRun:
    if fill not in PRICE_FILLS:
        raise W0Error(_BAD_FILL)
    monthly = bool(plugin.monthly_defaults and window.extra.get("monthly", False))
    c = InstrumentContext(
        universe=window.universe,
        pair=window.pair,
        asset=window.asset,
        timeframe=window.timeframe,
        monthly=monthly,
    )
    a = plugin.validate_parameters(params, c)
    b = plugin.check_compatibility(window)
    if not b.compatible:
        d = b.reason or getattr(plugin, "incompatible_reason", "incompatible calendar window")
        raise W0Error(d)
    delay, quote = PRICE_FILLS[fill]
    base_rows = (
        list(window.rows)
        if window.timeframe == "1h"
        else [r for r in window.rows if (r.get("min_volume") or 0) > 0]
    )
    rows = _rows_for_fill(base_rows, quote)
    e = CalendarScan(window, rows, a, delay, quote, monthly)
    trades = list(plugin.scan_trades(e))
    if (
        trades
        and window.rows
        and (trades[-1].get("reason") or "end_of_window") == "end_of_window"
        and trades[-1]["exit"]["date"] != window.rows[-1]["date"]
    ):
        final_row = window.rows[-1]
        trades[-1] = {
            **trades[-1],
            "exit": final_row,
            "exit_price": calendar_price(final_row, quote),
        }
    return calendar_intent_run(plugin.strategy_id, fill, a, window.rows, trades)


def calendar_entry_bands(
    plugin: Any,
    series: list[dict[str, Any]],
    params: Mapping[str, Any],
    fill: str,
    far_point_value_rub: float = 1.0,
) -> list[EntryBandPoint]:
    if fill not in PRICE_FILLS:
        raise W0Error(_BAD_FILL)
    a = plugin.validate_parameters(select_parameters(params, plugin.band_keys))
    return plugin.band_engine(series, a, fill, far_point_value_rub=far_point_value_rub)


def trade_costs(
    profile: dict[str, Any] | None, row: dict[str, Any], quantity: int, native_spread: bool
) -> dict[str, float]:
    if profile is None:
        return {"reference_flat": REFERENCE_ROUND_TRIP_COST / 2 * quantity}
    a = {**SPREAD_MULTIPLIER, "tariff_group": profile.get("tariff_group", "stock")}
    b = [
        ({**a, "secid": "near"}, float(row["near_close"])),
        ({**a, "secid": "far"}, float(row["far_close"])),
    ]
    return side_cost(profile, b, quantity, as_of=str(row["date"]), native_spread=native_spread)


def _benchmark_price_at(
    benchmark: dict[str, float], dates: list[str], observation: str | dict[str, Any]
) -> float:
    a = observation.get("benchmark_mark_date") if isinstance(observation, dict) else None
    b = observation.get("date") if isinstance(observation, dict) else observation
    c = str(a or b)[:10]
    d = (
        bisect_right(dates, c) - 1
        if a is not None or "T" not in str(b)
        else bisect_left(dates, c) - 1
    )
    return benchmark[dates[max(0, d)]]


def account_trades(
    label: str,
    per_pair: dict[str, list[dict[str, Any]]],
    benchmark: dict[str, float],
    profile: dict[str, Any] | None,
    **options: Any,
) -> dict[str, Any]:
    f = set(options) - _ACCOUNT_OPTS
    if f:
        raise TypeError(f"unexpected keyword argument(s): {sorted(f)}")
    native_spread = bool(options.get("native_spread", False))
    quantity = int(options.get("quantity", 1))
    benchmark_dates: list[str] | None = options.get("benchmark_dates")
    dates = benchmark_dates if benchmark_dates is not None else sorted(benchmark)
    c = d = e = 0.0
    trades: list[dict[str, Any]] = []
    for o, p in per_pair.items():
        for item in p:
            a, b = item["entry"], item["exit"]
            g = int(item["direction"])
            h = g * (float(item["exit_price"]) - float(item["entry_price"])) * quantity
            i = trade_costs(profile, a, quantity, native_spread)
            j = trade_costs(profile, b, quantity, native_spread)
            k = sum(i.values()) + sum(j.values())
            q = _benchmark_price_at(benchmark, dates, a)
            r = _benchmark_price_at(benchmark, dates, b)
            m = float(a["margin"]) * quantity * (r / q - 1)
            c += h
            d += k
            e += m
            trades.append(
                row_dict(
                    _TRADE_KEYS,
                    (
                        o,
                        a["date"],
                        b["date"],
                        g,
                        quantity,
                        item["entry_price"],
                        item["exit_price"],
                        round(h, 2),
                        round(k, 2),
                        i,
                        j,
                        item["reason"],
                        item["signal"],
                        round(float(a["margin"]) * quantity, 2),
                        round(m, 2),
                        a,
                        b,
                    ),
                )
            )
    n = c - d
    return row_dict(
        _ACCOUNT_KEYS,
        (
            label,
            len(trades),
            sum(a["gross_rub"] - a["costs_rub"] > 0 for a in trades),
            round(c, 2),
            round(d, 2),
            round(n, 2),
            round(e, 2),
            round(n - e, 2),
            trades,
        ),
    )


def strategy_runner(
    strategy: str,
    *,
    delay: int,
    fill: str,
    close_at_end: bool,
    skip_dividend_windows_in_zscore: bool,
    **options: Any,
) -> Callable[[list[dict[str, Any]]], list[dict[str, Any]]]:
    a = set(options) - _RUNNER_OPTS
    if a:
        raise TypeError(f"unexpected keyword argument(s): {sorted(a)}")
    b = float(options.get("threshold", 1.0))
    zscore_options: dict[str, Any] = options.get("zscore_options") or {}
    z_cache: dict[Any, Any] | None = options.get("z_cache")
    z_cache_key: Any = options.get("z_cache_key")
    if strategy == "convergence_to_ruonia":
        return lambda rows: convergence_trades(
            rows,
            threshold=b,
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
    a = set(options) - _EVAL_OPTS
    if a:
        raise TypeError(f"unexpected keyword argument(s): {sorted(a)}")
    native_spread = bool(options.get("native_spread", False))
    b = bool(options.get("reference_cost", True))
    c = bool(options.get("skip_dividend_windows_in_zscore", True))
    close_at_end = bool(options.get("close_at_end", False))
    zscore_options: dict[str, Any] | None = options.get("zscore_options")
    quantity = int(options.get("quantity", 1))
    variants: list[dict[str, Any]] = []
    for label, delay, fill in FILLS:
        for strategy in ("convergence_to_ruonia", "zscore_mean_reversion"):
            d = strategy_runner(
                strategy,
                delay=delay,
                fill=fill,
                close_at_end=close_at_end,
                skip_dividend_windows_in_zscore=c,
                zscore_options=zscore_options,
            )
            per_pair = {a: d(_rows_for_fill(rows, fill)) for (a, rows) in pairs.items()}
            if b:
                variants.append(
                    {
                        "strategy": strategy,
                        "cost_model": "reference_flat_round_trip",
                        **account_trades(label, per_pair, benchmark, None, quantity=quantity),
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
        b = json.loads(path.read_text(encoding="utf-8"))
        return {row["date"]: float(row["price"]) for row in b["rows"]}
    except (OSError, ValueError, KeyError) as a:
        raise W0Error(f"cannot read benchmark: {a}") from a


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
    "row_dict",
    "select_parameters",
    "strategy_runner",
    "trade_costs",
    "zscore_trades",
]
