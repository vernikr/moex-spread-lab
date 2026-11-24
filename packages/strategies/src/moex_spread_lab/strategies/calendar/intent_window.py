"""Row eligibility flags and the normalized snapshot handed to calendar plugin intents."""

from __future__ import annotations

from typing import Any, TypedDict

from moex_spread_lab.core.contracts import WindowSnapshot
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error


def prepare_daily_eligibility(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            # Shared with the plugin intents so cache telemetry and repeated-trial behaviour
            # match the historical runner exactly.
            "z_cache": z_cache,
            "z_cache_key": z_cache_key,
        },
    )


def build_h1_eligible_rows(
    window: dict[str, Any], fill: str, quantity: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply the hourly liquidity gate to every session row and flag its eligibility."""
    is_open = fill == "open"
    gate = float(window["liquidity_gate"])
    h1_rows: list[dict[str, Any]] = []
    for src in window["rows"]:
        row = dict(src)
        close_ok = bool(row.get("volume_eligible"))
        fill_ok = (
            float(row.get("liquidity_cumulative_before", 0.0)) >= gate
            and float(row.get("prior_bar_volume", 0.0)) >= quantity
            if is_open
            else close_ok and float(row.get("min_volume", 0.0)) >= quantity
        )
        row["signal_eligible"] = close_ok
        row["entry_eligible"] = bool(fill_ok)
        row["exit_eligible"] = bool(fill_ok)
        h1_rows.append(row)
    if sum(bool(r["signal_eligible"]) for r in h1_rows) < 2:
        raise W0Error("fewer than two causally liquidity-eligible hourly observations")
    return {**window, "rows": h1_rows}, h1_rows


__all__ = ["build_h1_eligible_rows", "intent_snapshot", "prepare_daily_eligibility"]
