"""Cost scenarios and cost breakdown helpers for analytical pair baselines."""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

from moex_spread_lab.accounting.costs import COST_COMPONENTS, COST_SCHEMA
from moex_spread_lab.core.w0 import W0Error

ANALYTICS_SCHEMA = "analytical-baseline.v1"
REQUIRED_COST_COMPONENTS = ("exchange", "clearing", "broker", "slippage", "funding", "exit")


def _scenario_dict(name: str, exch: float, fund: float) -> dict[str, Any]:
    return {
        "schema_version": "cost-profile.v1",
        "scenario": name,
        "status": "illustrative_unverified",
        "evidence_status": "account_tariff_and_historical_execution_unverified",
        "currency": "RUB",
        "components": {
            "exchange": {"value": exch, "unit": "rub_per_leg_side"},
            "clearing": {"value": exch, "unit": "rub_per_leg_side"},
            "broker": {"value": exch, "unit": "rub_per_leg_side"},
            "slippage": {"value": exch, "unit": "ticks_per_leg_side"},
            "funding": {"value": fund, "unit": "rub_per_leg_day"},
            "exit": {"value": exch, "unit": "rub_per_leg_side"},
        },
    }


COST_SCENARIOS: dict[str, dict[str, Any]] = {
    "low": _scenario_dict("low", 0.5, 0.25),
    "base": _scenario_dict("base", 1.0, 0.5),
    "stress": _scenario_dict("stress", 2.0, 1.0),
}


class AnalyticsError(W0Error):
    """Raised when analytical inputs cannot pass the baseline contract."""


def get_cost_scenario(name: str) -> dict[str, Any]:
    """Return a copy of an explicitly illustrative, unverified cost scenario."""
    try:
        return deepcopy(COST_SCENARIOS[name])
    except KeyError as exc:
        raise AnalyticsError(f"unknown cost scenario: {name}") from exc


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise AnalyticsError(f"{field} must be a finite number")
    number = float(value)
    if positive and number <= 0:
        raise AnalyticsError(f"{field} must be positive")
    return number


def _round(value: float) -> float:
    return round(float(value), 8)


def _validate_cost_profile(profile: dict[str, Any]) -> dict[str, Any]:
    if profile.get("schema_version") == COST_SCHEMA:
        return deepcopy(profile)
    if profile.get("schema_version") != "cost-profile.v1":
        raise AnalyticsError("unsupported cost profile schema")
    components = profile.get("components")
    if not isinstance(components, dict):
        raise AnalyticsError("cost profile components are required")
    normalized = deepcopy(profile)
    for component in REQUIRED_COST_COMPONENTS:
        item = normalized["components"].get(component)
        if not isinstance(item, dict):
            raise AnalyticsError(f"cost profile is missing {component}")
        item = cast(dict[str, Any], item)
        value = _number(item.get("value"), f"cost profile {component}")
        if value < 0:
            raise AnalyticsError(f"cost profile {component} cannot be negative")
        if not isinstance(item.get("unit"), str) or not item["unit"]:
            raise AnalyticsError(f"cost profile {component} unit is required")
        item["value"] = value
    return normalized


def _calendar_days(start_timestamp: str, end_timestamp: str) -> int:
    try:
        start = date.fromisoformat(start_timestamp[:10])
        end = date.fromisoformat(end_timestamp[:10])
    except (TypeError, ValueError) as exc:
        raise AnalyticsError("trade timestamps must start with ISO dates") from exc
    return max(1, (end - start).days)


def _trade_cost_breakdown(
    profile: dict[str, Any],
    *,
    entry_timestamp: str,
    exit_timestamp: str,
    quantity: int,
    near_contract: dict[str, Any],
    far_contract: dict[str, Any],
) -> dict[str, float]:
    components = profile["components"]
    legs = 2 * quantity
    slippage_ticks = float(components["slippage"]["value"])
    tick_sum = float(near_contract["tick_value"]) + float(far_contract["tick_value"])
    days = _calendar_days(entry_timestamp, exit_timestamp)
    return {
        "exchange": _round(float(components["exchange"]["value"]) * legs * 2),
        "clearing": _round(float(components["clearing"]["value"]) * legs * 2),
        "broker": _round(float(components["broker"]["value"]) * legs * 2),
        "slippage": _round(slippage_ticks * tick_sum * quantity * 2),
        "funding": _round(float(components["funding"]["value"]) * legs * days),
        "exit": _round(float(components["exit"]["value"]) * legs),
    }


@dataclass
class SimContext:
    params: Any
    near_contract: dict[str, Any]
    far_contract: dict[str, Any]
    near_multiplier: float
    far_multiplier: float
    margin_per_pair: float
    normalized_cost: dict[str, Any] | None
    tariff_costs: bool
    cost_components: tuple[str, ...]
    cost_status: str
    fast_intents: bool


def build_sim_context(series: dict[str, Any], params: Any) -> SimContext:
    rows = series.get("rows", [])
    if len(rows) < 2:
        raise AnalyticsError("fewer than two pair timestamps with benchmark marks")
    near_contract = series["contracts"][series["near"]]
    far_contract = series["contracts"][series["far"]]
    margin_per_pair = float(near_contract["initial_margin"]) + float(far_contract["initial_margin"])
    if not params.sizing_intents and margin_per_pair * params.quantity > params.initial_capital:
        raise AnalyticsError(
            f"ГО для выбранного размера ({margin_per_pair * params.quantity:.2f} ₽) превышает "
            f"стартовый капитал ({params.initial_capital:.2f} ₽). Допуск по стресс-тесту не пополняет счёт."
        )
    if params.fill == "open" and not all("near_open" in r and "far_open" in r for r in rows):
        raise AnalyticsError("open fills require open prices in the series")
    fast_intents = params.metrics_only and params.sizing_intents
    norm_cost = (
        None
        if fast_intents or params.cost_profile is None
        else _validate_cost_profile(params.cost_profile)
    )
    tariff_costs = norm_cost is not None and norm_cost["schema_version"] == COST_SCHEMA
    cost_status = (
        "unknown"
        if norm_cost is None
        else str(norm_cost["status"])
        if tariff_costs
        else "illustrative_unverified"
    )
    return SimContext(
        params=params,
        near_contract=near_contract,
        far_contract=far_contract,
        near_multiplier=float(near_contract["tick_value"]) / float(near_contract["tick_size"]),
        far_multiplier=float(far_contract["tick_value"]) / float(far_contract["tick_size"]),
        margin_per_pair=margin_per_pair,
        normalized_cost=norm_cost,
        tariff_costs=tariff_costs,
        cost_components=COST_COMPONENTS if tariff_costs else REQUIRED_COST_COMPONENTS,
        cost_status=cost_status,
        fast_intents=fast_intents,
    )


# Shared names inside the pair strategy; retain private entry points.
round_value = _round
trade_cost_breakdown = _trade_cost_breakdown
