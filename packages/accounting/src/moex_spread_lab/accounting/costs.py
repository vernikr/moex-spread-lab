"""Published MOEX derivatives taker tariff with a zero VTB broker fee, plus a slippage parameter."""

from __future__ import annotations

from typing import Any

from moex_spread_lab.core.w0 import W0Error

COST_SCHEMA = "cost-profile.v2"
COST_COMPONENTS = ("exchange_clearing", "broker", "slippage")
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


def tariff_profile(slippage_ticks: float = 1.0) -> dict[str, Any]:
    if (
        not isinstance(slippage_ticks, (int, float))
        or isinstance(slippage_ticks, bool)
        or not 0 <= slippage_ticks <= 10
    ):
        raise W0Error("slippage must be 0–10 ticks per leg and side")
    return {
        "schema_version": COST_SCHEMA,
        "scenario": "moex_taker_tariff_zero_broker",
        "status": "published_tariff_group_assignment_partly_assumed",
        "slippage_ticks": float(slippage_ticks),
        "broker_fee": 0.0,
        "broker_fee_effective_to": "2026-12-31",
        "taker_rate_percent": TAKER_RATE_PERCENT,
        "source": TARIFF_SOURCE,
    }


def side_cost(
    profile: dict[str, Any],
    legs: list[tuple[dict[str, Any], float]],
    quantity: int,
    *,
    native_spread: bool = False,
) -> dict[str, float]:
    """Cost of one entry or exit side across legs; each leg is (contract terms, fill price).

    A native calendar-spread order pays the tariff on both legs (the published rule) but crosses
    one spread book, so slippage is charged once, on the first leg's tick value.
    """
    exchange = slippage = 0.0
    for position, (contract, price) in enumerate(legs):
        group = contract.get("tariff_group") or CONTRACT_GROUP.get(contract["secid"])
        if group is None:
            raise W0Error(f"no tariff group assigned for {contract['secid']}")
        value = price * contract["tick_value"] / contract["tick_size"]
        exchange += value * TAKER_RATE_PERCENT[group] / 100 * quantity
        if not native_spread or position == 0:
            slippage += profile["slippage_ticks"] * contract["tick_value"] * quantity
    return {"exchange_clearing": exchange, "broker": 0.0, "slippage": slippage}


def _cost_totals(trades: list[dict[str, Any]]) -> dict[str, float]:
    totals = {"exchange_clearing": 0.0, "broker": 0.0, "slippage": 0.0}
    for trade in trades:
        for side in ("entry", "exit"):
            for key, value in (trade[side].get("costs") or {}).items():
                if key in totals:
                    totals[key] += float(value)
    return {key: round(value, 2) for key, value in totals.items()}
