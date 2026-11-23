"""Causal contract terms and maximum-affordable quantity calculation."""

from __future__ import annotations

import math
from typing import Any

from moex_spread_lab.core.w0 import W0Error

from .account_risk import DEFAULT_STRESS_VM_BUFFER, DEFAULT_UNCERTAINTY_BUFFER
from .costs import side_cost, tariff_profile

AUTO_SIZING = "max_affordable"
SIZING_POLICY = "max-affordable.v1"


def automatic(request: dict[str, Any]) -> bool:
    return request.get("sizing") == AUTO_SIZING


def _positive(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise W0Error(f"Нет пригодного значения для расчёта размера: {name}.")
    return float(value)


def _money(value: float) -> float:
    return round(value, 2)


def _required_margin_and_reserves(
    margin: float, *, stress: bool, native_spread: bool
) -> tuple[float, float]:
    fixed_reserve = DEFAULT_UNCERTAINTY_BUFFER if stress and not native_spread else 0.0
    per_pair_reserve = (
        DEFAULT_STRESS_VM_BUFFER + (DEFAULT_UNCERTAINTY_BUFFER if native_spread else 0.0)
        if stress
        else 0.0
    )
    required_margin = (3 if stress else 1) * margin + per_pair_reserve
    return required_margin, fixed_reserve


def maximum_quantity(
    budget: float,
    margin: float,
    unit_costs: dict[str, float],
    *,
    stress: bool,
    native_spread: bool = False,
) -> int:
    """Largest integer paying margin, entry and an entry-priced exit-cost reserve."""
    margin_val = _positive(margin, "ГО пары")
    if not math.isfinite(budget) or budget <= 0:
        return 0
    if any(not math.isfinite(v) or v < 0 for v in unit_costs.values()):
        raise W0Error("Неизвестные издержки не могут стать нулём при расчёте размера.")
    required_margin, fixed_reserve = _required_margin_and_reserves(
        margin_val, stress=stress, native_spread=native_spread
    )

    def requirement(qty: int) -> float:
        fees = sum(_money(v * qty) for v in unit_costs.values())
        return required_margin * qty + fixed_reserve + 2 * fees

    qty = max(
        0,
        math.floor((budget - fixed_reserve) / (required_margin + 2 * sum(unit_costs.values()))),
    )
    # Component rounding can move the exact boundary by a kopeck.
    while qty and requirement(qty) > budget + 1e-9:
        qty -= 1
    while requirement(qty + 1) <= budget + 1e-9:
        qty += 1
    return qty


class _Terms:
    def __init__(self, proof: dict[str, Any], contracts: dict[str, Any], settings: Any) -> None:
        self.native = proof.get("universe") == "calendar_native"
        self.native_order = (
            self.native and proof.get("selection", {}).get("price_source") != "synthetic_leg_proxy"
        )
        self.rows: list[dict[str, Any]] = proof["series"]
        self.open_fill = proof["selection"].get("fill") == "next_open"
        self.profile = tariff_profile(settings.slippage_ticks)
        self.contract: dict[str, Any] = proof["contract"]
        if self.native:
            self.rate = _positive(self.contract.get("margin_rate"), "коэффициент ГО")
            self.tick_size = _positive(self.contract.get("tick_size"), "шаг цены")
            self.reference_point = _positive(
                self.contract.get("point_value_rub"), "стоимость пункта"
            )
            self.group: str = self.contract["tariff_group"]
            self.legs: list[dict[str, Any]] = []
            self.margin = 0.0
        else:
            instrument = proof["instrument"]
            try:
                self.legs = [contracts[instrument[k]] for k in ("near", "far")]
            except KeyError as exc:
                raise W0Error("Нет параметров обеих ног для расчёта размера.") from exc
            self.margin = sum(_positive(c.get("initial_margin"), "ГО ноги") for c in self.legs)
            for leg in self.legs:
                _positive(leg.get("tick_size"), "шаг цены ноги")
                _positive(leg.get("tick_value"), "стоимость шага ноги")

    def point(self, index: int) -> float:
        return (
            _positive(self.rows[index].get("point_value_rub"), "дневная стоимость пункта")
            if self.native
            else 1.0
        )

    def fill_point(self, index: int, *, at_open: bool) -> float:
        if self.native and at_open:
            return self.point(index - 1) if index else self.reference_point
        return self.point(index)

    def at_fill(
        self, fill: dict[str, Any], index: int, *, at_open: bool
    ) -> tuple[float, dict[str, float]]:
        near = _positive(fill.get("near_price"), "near_price")
        far = _positive(fill.get("far_price"), "far_price")
        point = self.fill_point(index, at_open=at_open)
        if self.native:
            common = {
                "tick_size": self.tick_size,
                "tick_value": point * self.tick_size,
                "tariff_group": self.group,
            }
            legs = [({**common, "secid": "near"}, near), ({**common, "secid": "far"}, far)]
            margin = self.rate * (near + far) * point
        else:
            legs = list(zip(self.legs, (near, far), strict=True))
            margin = self.margin
        return margin, side_cost(self.profile, legs, 1, native_spread=self.native_order)

    def posted_margin(self, index: int, quantity: int) -> float:
        if self.native:
            row = self.rows[index]
            return self.rate * (row["near"] + row["far"]) * self.point(index) * quantity
        return self.margin * quantity


# Public internal-package names; the private names remain for compatibility.
positive = _positive
money = _money
Terms = _Terms
