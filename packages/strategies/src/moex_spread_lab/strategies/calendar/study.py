"""Daily calendar-window construction and multi-asset study runner."""

from __future__ import annotations

import itertools
from bisect import bisect_right
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.costs import tariff_profile
from moex_spread_lab.core.settings import INITIAL_CAPITAL, MIN_DAILY_VOLUME

from ..calendar_eval import evaluate_variants, read_benchmark
from ..calendar_ruonia.engine import implied_ruonia_deviation_pp, window_dividend_rub
from .contracts import (
    ASSETS,
    CROSS_EXPIRY_PAIRS,
    EXIT_BUFFER_DAYS,
    FILL_LABELS,
    FX_SOURCE,
    HISTORY_FIELDS,
    HISTORY_FROM,
    MIN_H1_CALENDAR_OBSERVATIONS,
    MIN_H1_NATIVE_SESSION_VOLUME,
    MIN_H1_PROXY_LEG_SESSION_VOLUME,
    MONTH_CODES,
    QUARTER_CODES,
    STRATEGIES,
    SYNTHETIC_TRIANGLE_PAIRS,
    ZSCORE_OPTIONS,
    contract_chain,
    spread_id,
)
from .contracts import (
    calendar_strategy_runner as _calendar_strategy_runner,
)
from .contracts import (
    fx_factors as _fx_factors,
)
from .contracts import (
    positive_spread_volume_rows as _positive_spread_volume_rows,
)
from .contracts import (
    ruonia as _ruonia,
)
from .contracts import (
    stress_ok as _stress_ok,
)
from .contracts import (
    terms as _terms,
)
from .contracts import (
    zscore_options as _zscore_options,
)
from .loaders import cached_till_date, fetch_inputs
from .loaders import offline_loader as _offline


def _build_daily_window_row(
    d: str,
    day_sources: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    terms: dict[str, Any],
    fx_val: float,
    win_ctx: tuple[date, float, float, bool],
) -> dict[str, Any]:
    near_row, far_row, nat_row = day_sources
    expiry_date, dividend, rate, conv_ok = win_ctx
    mult = float(terms["mult"]) * fx_val
    near_px, far_px = float(near_row["SETTLEPRICE"]), float(far_row["SETTLEPRICE"])
    near_open = float(near_row["OPEN"] or 0)
    far_open = float(far_row["OPEN"] or 0)
    near_value, far_value = near_px * mult, far_px * mult
    spread = far_value - near_value
    return {
        "date": d,
        "spread": spread,
        "open_spread": (far_open - near_open) * mult if near_open > 0 and far_open > 0 else spread,
        "near_close": near_value,
        "far_close": far_value,
        "dev_pp": implied_ruonia_deviation_pp(
            spread, near_value, dividend, rate, convergence_supported=conv_ok
        ),
        "days_to_near_expiry": (expiry_date - date.fromisoformat(d)).days,
        "dividend_in_window": dividend > 0,
        "margin": float(terms["margin_rate"]) * (near_value + far_value),
        "min_volume": float(nat_row["VOLUME"] or 0),
        "mult": mult,
        "near_price": near_px,
        "far_price": far_px,
        "near_open_price": near_open,
        "far_open_price": far_open,
        "spread_price": far_px - near_px,
        "spread_open_price": far_open - near_open
        if near_open > 0 and far_open > 0
        else far_px - near_px,
        "spread_last_trade": nat_row.get("CLOSE"),
        "spread_trades": nat_row.get("NUMTRADES"),
        "near_volume": near_row.get("VOLUME"),
        "far_volume": far_row.get("VOLUME"),
    }


def build_windows(
    histories: dict[str, dict[str, dict[str, Any]]],
    asset: str,
    terms: dict[str, Any],
    ruonia: tuple[list[str], dict[str, float]],
    factors: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """One window per complete contract cycle: days after previous near expiry through two days before this one."""
    config = ASSETS[asset]
    chain = contract_chain(asset)
    last_date = max(max(h) for h in histories.values() if h)
    ruonia_dates, rates = ruonia
    fx = (factors or {}).get(config.get("fx", ""), {})
    min_rows = 12 if config.get("monthly") else 25
    windows: list[dict[str, Any]] = []
    previous_expiry: str | None = None
    conv_ok = bool(config.get("convergence"))
    for near, far in itertools.pairwise(chain):
        legs, native, far_rows = (
            histories.get(near),
            histories.get(spread_id(near, far)),
            histories.get(far),
        )
        if not legs or not native or not far_rows:
            continue
        expiry = max(legs)
        if expiry >= last_date:
            break
        expiry_date = date.fromisoformat(expiry)
        cutoff = (expiry_date - timedelta(days=EXIT_BUFFER_DAYS)).isoformat()
        days = [
            d
            for d in sorted(native)
            if (previous_expiry is None or d > previous_expiry)
            and d <= cutoff
            and d in legs
            and d in far_rows
            and legs[d]["SETTLEPRICE"]
            and far_rows[d]["SETTLEPRICE"]
            and (not config.get("fx") or d in fx)
        ]
        previous_expiry = expiry
        if len(days) < min_rows:
            continue
        dividend = window_dividend_rub(asset, expiry_date)
        rows = [
            _build_daily_window_row(
                d,
                (legs[d], far_rows[d], native[d]),
                terms,
                fx.get(d, 1.0),
                (
                    expiry_date,
                    dividend,
                    rates[ruonia_dates[max(0, bisect_right(ruonia_dates, d) - 1)]],
                    conv_ok,
                ),
            )
            for d in days
        ]
        eligible_volumes = [float(r["min_volume"]) for r in _positive_spread_volume_rows(rows)]
        windows.append(
            {
                "pair": f"{near}/{far}",
                "spread_instrument": spread_id(near, far),
                "near": near,
                "far": far,
                "first_date": days[0],
                "last_date": days[-1],
                "near_expiry": expiry,
                "rows": rows,
                "min_spread_volume": min(eligible_volumes) if eligible_volumes else 0.0,
                "max_pair_margin_rub": round(max(float(r["margin"]) for r in rows), 2),
            }
        )
    return windows


def _span_years(windows: list[dict[str, Any]]) -> float:
    return (
        date.fromisoformat(windows[-1]["near_expiry"])
        - date.fromisoformat(windows[0]["first_date"])
    ).days / 365


def _evaluate_asset_study(
    asset: str,
    windows: list[dict[str, Any]],
    benchmark: dict[str, float],
    profile: dict[str, Any],
) -> dict[str, Any]:
    if not windows:
        return {"status": "no_complete_windows"}
    eligible = {w["pair"]: w["rows"] for w in windows if w["min_spread_volume"] >= MIN_DAILY_VOLUME}
    years = _span_years(windows)
    raw_variants = (
        evaluate_variants(
            eligible,
            benchmark,
            {**profile, "tariff_group": ASSETS[asset]["group"]},
            native_spread=True,
            reference_cost=False,
            skip_dividend_windows_in_zscore=False,
            close_at_end=True,
            zscore_options=_zscore_options(asset),
        )
        if eligible
        else []
    )
    variants: list[dict[str, Any]] = []
    for variant in raw_variants:
        by_window: dict[str, float] = {}
        for trade in variant.pop("trade_list"):
            by_window[trade["pair"]] = (
                by_window.get(trade["pair"], 0.0) + trade["gross_rub"] - trade["costs_rub"]
            )
        variants.append(
            {
                **variant,
                "windows_with_trades": len(by_window),
                "windows_net_positive": sum(v > 0 for v in by_window.values()),
                "excess_pp_per_year_on_allocation": round(
                    variant["excess_over_lqdt_on_margin_rub"] / INITIAL_CAPITAL / years * 100, 3
                ),
            }
        )
    return {
        "status": "evaluated",
        "name": ASSETS[asset]["name"],
        "tariff_group": ASSETS[asset]["group"],
        "span": {
            "from": windows[0]["first_date"],
            "to": windows[-1]["near_expiry"],
            "years": round(years, 2),
        },
        "windows": [
            {
                "pair": w["pair"],
                "spread_instrument": w["spread_instrument"],
                "from": w["first_date"],
                "to": w["last_date"],
                "observations": len(w["rows"]),
                "min_spread_volume": w["min_spread_volume"],
                "tradable_every_day": w["min_spread_volume"] >= MIN_DAILY_VOLUME,
                "max_pair_margin_rub": w["max_pair_margin_rub"],
                "passes_capital_screen": _stress_ok(w["max_pair_margin_rub"]),
            }
            for w in windows
        ],
        "variants": variants,
    }


def run_calendar_study(
    cache_dir: Path,
    benchmark_path: Path,
    benchmark_cache_dir: Path,
    contracts_path: Path,
    **options: Any,
) -> dict[str, Any]:
    assets: tuple[str, ...] = options.get("assets", tuple(ASSETS))
    slippage_ticks = float(options.get("slippage_ticks", 1.0))
    till_date: str = options["till_date"]
    fetch_fn: Callable[..., dict[str, dict[str, dict[str, Any]]]] = options.get(
        "fetch_fn", fetch_inputs
    )
    histories = fetch_fn(cache_dir, assets, till_date)
    benchmark = read_benchmark(benchmark_path)
    ruonia = _ruonia(benchmark_cache_dir / "CBR_RUONIA.json")
    terms = _terms(contracts_path, assets)
    factors = _fx_factors(histories)
    profile = tariff_profile(slippage_ticks)
    report = {
        asset: _evaluate_asset_study(
            asset,
            build_windows(histories, asset, terms[asset], ruonia, factors)
            if asset in terms
            else [],
            benchmark,
            profile,
        )
        for asset in assets
    }
    return {
        "schema_version": "calendar-spread-study.v2",
        "release_state": "exploratory_inconclusive",
        "pricing": "spread = far leg settlement minus near leg settlement (identical to the exchange spread settlement where published); opens are leg opens",
        "execution": "native spread order: tariff on both legs, slippage once per side; next_open is the primary fill",
        "volume_rule": f"every day of the front window needs at least {MIN_DAILY_VOLUME} contracts in the spread instrument",
        "margin": "current margin-to-notional ratio of the live contract applied to historical settlements (fallback, A-05)",
        "fx": "USD- and CNY-quoted point values are rescaled by the front Si or CR settlement relative to today (approximation)",
        "dividends": "SBER ex-dates from a third-party seed database, unverified; none for other stocks (convergence runs only for SR and GZ)",
        "benchmark": "LQDT lineage; excess subtracts LQDT return on pair margin while a position is open",
        "assets": report,
    }


__all__ = [
    "ASSETS",
    "CROSS_EXPIRY_PAIRS",
    "EXIT_BUFFER_DAYS",
    "FILL_LABELS",
    "FX_SOURCE",
    "HISTORY_FIELDS",
    "HISTORY_FROM",
    "MIN_H1_CALENDAR_OBSERVATIONS",
    "MIN_H1_NATIVE_SESSION_VOLUME",
    "MIN_H1_PROXY_LEG_SESSION_VOLUME",
    "MONTH_CODES",
    "QUARTER_CODES",
    "STRATEGIES",
    "SYNTHETIC_TRIANGLE_PAIRS",
    "ZSCORE_OPTIONS",
    "_calendar_strategy_runner",
    "_fx_factors",
    "_offline",
    "_positive_spread_volume_rows",
    "_ruonia",
    "_span_years",
    "_stress_ok",
    "_terms",
    "_zscore_options",
    "build_windows",
    "cached_till_date",
    "contract_chain",
    "fetch_inputs",
    "run_calendar_study",
    "spread_id",
]
