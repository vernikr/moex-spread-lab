"""Published MOEX derivatives taker tariff, zero VTB broker fee, slippage, and LQDT overlay.

Broker fee unit: RUB per contract per side, as published by the VTB tariff in force
until `broker_fee_effective_to` (A-09). Beyond that date the zero-fee promotion is
unevidenced, so pricing fails closed instead of charging zero.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, NamedTuple, cast

from moex_spread_lab.core.w0 import W0Error

COST_SCHEMA = "cost-profile.v2"
COST_COMPONENTS = ("exchange_clearing", "broker", "slippage")
# RUB: cash below this is float noise from the daily walk, not an unfunded account.
SHORTFALL_TOLERANCE = 1e-7
TARIFF_SOURCE = "https://www.moex.com/s93 (fetched 2026-11-08); docs/inputs/moy_online_praym.pdf"
TAKER_RATE_PERCENT = {
    "currency": 0.00462,
    "interest": 0.01650,
    "stock": 0.01980,
    "index": 0.00660,
    "commodity": 0.01320,
    "perpetual_stock": 0.03000,
    "digital": 0.04000,
}
CONTRACT_GROUP = {
    **dict.fromkeys(("CNYRUBF", "CRZ6", "CRH7", "USDRUBF", "SiZ6", "EURRUBF", "EuZ6"), "currency"),
    **dict.fromkeys(("GLDRUBF", "GLZ6", "GLH7", "SLVRUBF", "S2Z6"), "commodity"),
    **dict.fromkeys(("IMOEXF", "MMZ6"), "index"),
    **dict.fromkeys(("SBERF", "GAZPF"), "perpetual_stock"),
    **dict.fromkeys(("SRZ6", "GZZ6"), "stock"),
    **dict.fromkeys(("RGBIF", "RBZ6"), "interest"),
    **dict.fromkeys(("BTCUSDF", "BTZ6"), "digital"),
}


def tariff_profile(
    slippage_ticks: object = 1.0,
    *,
    broker_fee: object = 0.0,
    broker_fee_effective_to: object = "2026-12-31",
) -> dict[str, Any]:
    if (
        not isinstance(slippage_ticks, (int, float))
        or isinstance(slippage_ticks, bool)
        or not 0 <= slippage_ticks <= 10
    ):
        raise W0Error("slippage must be 0–10 ticks per leg and side")
    if (
        not isinstance(broker_fee, (int, float))
        or isinstance(broker_fee, bool)
        or not 0 <= broker_fee < float("inf")
    ):
        raise W0Error("broker fee must be a finite non-negative RUB amount per contract")
    if not isinstance(broker_fee_effective_to, str):
        raise W0Error("broker fee window must be an ISO date")
    try:
        date.fromisoformat(broker_fee_effective_to)
    except ValueError as exc:
        raise W0Error("broker fee window must be an ISO date") from exc
    return {
        "schema_version": COST_SCHEMA,
        "scenario": "moex_taker_tariff_zero_broker",
        "status": "published_tariff_group_assignment_partly_assumed",
        "slippage_ticks": float(slippage_ticks),
        "broker_fee": float(broker_fee),
        "broker_fee_unit": "rub_per_contract_per_side",
        "broker_fee_effective_to": broker_fee_effective_to,
        "taker_rate_percent": TAKER_RATE_PERCENT,
        "source": TARIFF_SOURCE,
    }


def _taker_rates(profile: dict[str, Any]) -> dict[str, float]:
    raw = profile.get("taker_rate_percent")
    if not isinstance(raw, dict) or not raw:
        raise W0Error("cost profile is missing taker_rate_percent")
    rates: dict[str, float] = {}
    for group, rate in cast(dict[str, Any], raw).items():
        if (
            not isinstance(rate, (int, float))
            or isinstance(rate, bool)
            or not 0 <= rate < float("inf")
        ):
            raise W0Error(f"taker rate for {group} must be a finite non-negative percent")
        rates[str(group)] = float(rate)
    return rates


def _broker_fee(profile: dict[str, Any], as_of: date) -> float:
    fee = profile.get("broker_fee")
    window_end = profile.get("broker_fee_effective_to")
    if not isinstance(fee, (int, float)) or isinstance(fee, bool) or not 0 <= fee < float("inf"):
        raise W0Error("broker fee must be a finite non-negative RUB amount per contract")
    if not isinstance(window_end, str):
        raise W0Error("cost profile is missing the broker fee window")
    if as_of > date.fromisoformat(window_end):
        raise W0Error(
            f"broker fee assumption expires {window_end}; supply an evidenced tariff_profile "
            "with an explicit broker_fee and broker_fee_effective_to"
        )
    return float(fee)


def side_cost(
    profile: dict[str, Any],
    legs: list[tuple[dict[str, Any], float]],
    quantity: int,
    *,
    as_of: str,
    native_spread: bool = False,
) -> dict[str, float]:
    """Native calendar-spread orders pay tariff on both legs but cross one spread book for slippage."""
    try:
        trade_date = date.fromisoformat(str(as_of)[:10])
    except ValueError as exc:
        raise W0Error("side cost requires an ISO trade date") from exc
    rates = _taker_rates(profile)
    broker_fee = _broker_fee(profile, trade_date)
    exchange = slippage = 0.0
    for position, (contract, price) in enumerate(legs):
        group = contract.get("tariff_group") or CONTRACT_GROUP.get(contract["secid"])
        if group is None:
            raise W0Error(f"no tariff group assigned for {contract['secid']}")
        if group not in rates:
            raise W0Error(f"cost profile has no taker rate for {group}")
        value = price * contract["tick_value"] / contract["tick_size"]
        exchange += value * rates[group] / 100 * quantity
        if not native_spread or position == 0:
            slippage += profile["slippage_ticks"] * contract["tick_value"] * quantity
    return {
        "exchange_clearing": exchange,
        "broker": broker_fee * quantity * len(legs),
        "slippage": slippage,
    }


def cost_totals(trades: list[dict[str, Any]]) -> dict[str, float]:
    totals = {"exchange_clearing": 0.0, "broker": 0.0, "slippage": 0.0}
    for trade in trades:
        for side in ("entry", "exit"):
            costs: dict[str, Any] = trade[side].get("costs") or {}
            for key, value in costs.items():
                if key in totals:
                    totals[key] += float(value)
    return {key: round(value, 2) for key, value in totals.items()}


class LqdtOverlay(NamedTuple):
    """Daily free-cash walk with the shortfall evidence used to reject a run."""

    values: list[float]
    shortfall_rows: int
    first_shortfall_index: int | None
    max_deficit: float


def _overlay_series(
    name: str, series: object, *, minimum: float | None = None, strict: bool = False
) -> list[float]:
    if not isinstance(series, list) or not series:
        raise W0Error(f"LQDT overlay {name} cannot be empty")
    values: list[float] = []
    for value in cast(list[Any], series):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise W0Error(f"LQDT overlay {name} must be finite numbers")
        if not math.isfinite(value):
            raise W0Error(f"LQDT overlay {name} must be finite numbers")
        if minimum is not None and (value <= minimum if strict else value < minimum):
            raise W0Error(f"LQDT overlay {name} must be {'>' if strict else '>='} {minimum}")
        values.append(float(value))
    return values


def _overlay_inputs(
    capital: object,
    cumulative_pnl: object,
    margins: object,
    benchmark_prices: object,
) -> tuple[float, list[float], list[float], list[float]]:
    if (
        not isinstance(capital, (int, float))
        or isinstance(capital, bool)
        or not math.isfinite(capital)
        or capital <= 0
    ):
        raise W0Error("LQDT overlay capital must be a finite positive RUB amount")
    pnl = _overlay_series("cumulative pnl", cumulative_pnl)
    held = _overlay_series("margins", margins, minimum=0.0)
    prices = _overlay_series("benchmark prices", benchmark_prices, minimum=0.0, strict=True)
    if not len(pnl) == len(held) == len(prices):
        raise W0Error("LQDT overlay series must align row by row")
    return float(capital), pnl, held, prices


def lqdt_overlay_values(
    capital: float,
    cumulative_pnl: list[float],
    margins: list[float],
    benchmark_prices: list[float],
) -> LqdtOverlay:
    """Free cash compounds with LQDT; reserved margin stays idle in cash (A-11).

    A negative free balance is an unfunded account, not LQDT-financed credit: it
    earns no benchmark return and is reported as a deficit so the caller can
    reject the run instead of letting the shortfall compound like a loan.
    """
    free, cumulative_pnl, margins, benchmark_prices = _overlay_inputs(
        capital, cumulative_pnl, margins, benchmark_prices
    )
    values: list[float] = []
    shortfall_rows = 0
    first_shortfall_index: int | None = None
    max_deficit = 0.0
    for index, pnl in enumerate(cumulative_pnl):
        growth = benchmark_prices[index] / benchmark_prices[index - 1] if index else 1.0
        previous_pnl = cumulative_pnl[index - 1] if index else 0.0
        previous_margin = margins[index - 1] if index else 0.0
        if free > 0:
            free *= growth
        free += (pnl - previous_pnl) - (margins[index] - previous_margin)
        if free < -SHORTFALL_TOLERANCE:
            shortfall_rows += 1
            if first_shortfall_index is None:
                first_shortfall_index = index
            max_deficit = max(max_deficit, -free)
        values.append(free + margins[index])
    return LqdtOverlay(values, shortfall_rows, first_shortfall_index, max_deficit)
