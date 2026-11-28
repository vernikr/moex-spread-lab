"""Validated fill terms and the columnar C99 metrics-only sizing engine."""

from __future__ import annotations

import math
from array import array
from typing import Any, cast

from moex_spread_lab.core.w0 import W0Error

from .account_risk import DEFAULT_STRESS_VM_BUFFER, DEFAULT_UNCERTAINTY_BUFFER
from .costs import TAKER_RATE_PERCENT, side_cost, tariff_profile

_COST_KEYS = ("exchange_clearing", "broker", "slippage")

type AcceptedTrade = tuple[int, int, float, float, float, float, float, float, float, float, int]

type ScanColumns = tuple[
    list[str],
    array[float],
    array[float],
    array[float],
    array[float],
    array[int],
    array[int],
    array[int],
    dict[str, int],
]


def positive(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise W0Error(f"Нет пригодного значения для расчёта размера: {name}.")
    return float(value)


def carry_settlement(proof: dict[str, Any]) -> bool:
    return (
        proof["selection"]["strategy"] == "funding_only"
        or proof.get("accounting_policy") == "carry_settlement.v1"
    )


def _validate_costs(costs: dict[str, float]) -> None:
    if any(not math.isfinite(v) or v < 0 for v in costs.values()):
        raise W0Error("Неизвестные издержки не могут стать нулём при расчёте размера.")


def maximum_quantity(
    budget: float,
    margin: float,
    unit_costs: dict[str, float],
    *,
    stress: bool,
    native_spread: bool = False,
) -> int:
    """Largest integer paying margin, entry and an entry-priced exit-cost reserve."""
    margin = positive(margin, "ГО пары")
    if not math.isfinite(budget) or budget <= 0:
        return 0
    _validate_costs(unit_costs)
    fixed = DEFAULT_UNCERTAINTY_BUFFER if stress and not native_spread else 0.0
    reserve = (
        DEFAULT_STRESS_VM_BUFFER + (DEFAULT_UNCERTAINTY_BUFFER if native_spread else 0.0)
        if stress
        else 0.0
    )
    required = (3 if stress else 1) * margin + reserve

    def requirement(qty: int) -> float:
        return required * qty + fixed + 2 * sum(round(v * qty, 2) for v in unit_costs.values())

    qty = max(0, math.floor((budget - fixed) / (required + 2 * sum(unit_costs.values()))))
    # Component rounding can move the exact boundary by a kopeck.
    while qty and requirement(qty) > budget + 1e-9:
        qty -= 1
    while requirement(qty + 1) <= budget + 1e-9:
        qty += 1
    return qty


def hydrate_trade(
    trade: dict[str, Any],
    marks: dict[str, float],
    costs: float | None = None,
    *,
    funding: float | None = None,
    native: bool = False,
) -> None:
    if costs is None:
        costs = sum(sum(trade[side]["costs"].values()) for side in ("entry", "exit"))
    trade["costs_rub"] = round(costs, 2)
    trade["net_rub"] = round(trade["gross_rub"] - trade["costs_rub"], 2)
    trade["lqdt_on_margin_rub"] = round(
        trade["margin_rub"] * (marks[trade["exit"]["date"]] / marks[trade["entry"]["date"]] - 1), 2
    )
    if funding is None:
        return
    if trade.get("funding_rub") is not None or not native:
        trade["funding_rub"] = funding
    trade["excess_rub"] = round(trade["net_rub"] - trade["lqdt_on_margin_rub"], 2)
    trade["result"] = "win" if trade["net_rub"] > 0 else "loss" if trade["net_rub"] < 0 else "flat"
    if "gross_rub_static" in trade:
        trade["gross_rub_static"] = round(trade["gross_rub_static"] * trade["quantity"], 2)


class Terms:
    def __init__(self, proof: dict[str, Any], contracts: dict[str, Any], settings: Any) -> None:
        self.settings = settings
        self.native = proof.get("universe") == "calendar_native"
        self.native_order = (
            self.native and proof["selection"].get("price_source") != "synthetic_leg_proxy"
        )
        self.rows: list[dict[str, Any]] = proof["series"]
        self.open_fill = proof["selection"].get("fill") == "next_open"
        self.profile = tariff_profile(settings.slippage_ticks)
        contract = proof["contract"]
        self.legs: list[dict[str, Any]] = []
        self.margin = 0.0
        if self.native:
            self.rate = positive(contract.get("margin_rate"), "коэффициент ГО")
            self.tick_size = positive(contract.get("tick_size"), "шаг цены")
            self.reference_point = positive(contract.get("point_value_rub"), "стоимость пункта")
            self.group = self._tariff_group(contract)
        else:
            instrument = proof["instrument"]
            try:
                self.legs = [contracts[instrument[k]] for k in ("near", "far")]
            except KeyError as exc:
                raise W0Error("Нет параметров обеих ног для расчёта размера.") from exc
            self.margin = sum(positive(c.get("initial_margin"), "ГО ноги") for c in self.legs)
            for leg in self.legs:
                positive(leg.get("tick_size"), "шаг цены ноги")
                positive(leg.get("tick_value"), "стоимость шага ноги")

    def _tariff_group(self, contract: dict[str, Any]) -> str:
        group = contract.get("tariff_group")
        if not isinstance(group, str) or group not in TAKER_RATE_PERCENT:
            raise W0Error(f"Нет тарифной группы у контракта для расчёта размера: {group!r}.")
        return group

    def point(self, index: int) -> float:
        return (
            positive(self.rows[index].get("point_value_rub"), "дневная стоимость пункта")
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
        near = positive(fill.get("near_price"), "near_price")
        far = positive(fill.get("far_price"), "far_price")
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
        return margin, side_cost(
            self.profile,
            legs,
            1,
            as_of=str(self.rows[index]["date"]),
            native_spread=self.native_order,
        )

    def posted_margin(self, index: int, quantity: int) -> float:
        if self.native:
            row = self.rows[index]
            return self.rate * (row["near"] + row["far"]) * self.point(index) * quantity
        return self.margin * quantity


def _scan_terms_shape(terms: Terms) -> tuple[Any, ...]:
    return (terms.native, terms.margin, getattr(terms, "rate", 0.0))


def _get_or_build_scan_columns(
    rows: list[dict[str, Any]],
    terms: Terms,
    marks: dict[str, float],
    scan_cache: dict[Any, tuple[Any, ...]] | None,
    scan_key: Any,
) -> ScanColumns:
    n = len(rows)
    entry = scan_cache.get(scan_key) if scan_cache is not None else None
    if (
        entry is not None
        and entry[0] is rows
        and entry[1] is marks
        and entry[2] == _scan_terms_shape(terms)
    ):
        return cast(ScanColumns, entry[3])
    dates = [r["date"] for r in rows]
    prices = array("d", (positive(marks[d], "отметка LQDT") for d in dates))
    spreads = array("d", (float(r["spread"]) for r in rows))
    if terms.native:
        points = array("d", (terms.point(i) for i in range(n)))
        posted_units = array("d", (terms.posted_margin(i, 1) for i in range(n)))
    else:
        points = array("d", [1.0]) * n
        posted_units = array("d", [terms.margin]) * n
    elig, entry_elig, exit_elig = (
        array("B", (bool(r.get(key, r.get("volume_eligible", True))) for r in rows))
        for key in ("volume_eligible", "entry_eligible", "exit_eligible")
    )
    date_to_idx = {d: i for i, d in enumerate(dates)}
    cols: ScanColumns = (
        dates,
        prices,
        spreads,
        points,
        posted_units,
        elig,
        entry_elig,
        exit_elig,
        date_to_idx,
    )
    if scan_cache is not None:
        scan_cache[scan_key] = (rows, marks, _scan_terms_shape(terms), cols)
    return cols


def _build_single_trade_spec(
    trade: dict[str, Any],
    orig_idx: int,
    entry_idx: int,
    exit_idx: int,
    terms: Terms,
) -> tuple[Any, ...] | None:
    same_day = exit_idx == entry_idx
    open_at_end = bool(trade.get("open_at_end"))
    entry_margin, entry_costs = terms.at_fill(trade["entry"], entry_idx, at_open=terms.open_fill)
    if not entry_costs.keys() <= set(_COST_KEYS):
        return None
    positive(entry_margin, "ГО пары")
    _validate_costs(entry_costs)
    if exit_idx >= 0:
        _, exit_costs = terms.at_fill(
            trade["exit"],
            exit_idx,
            at_open=False if same_day else (terms.open_fill and not open_at_end),
        )
        if not exit_costs.keys() <= set(_COST_KEYS):
            return None
        exit_point = (
            terms.point(exit_idx)
            if same_day
            else terms.fill_point(exit_idx, at_open=terms.open_fill and not open_at_end)
        )
    else:
        exit_costs = {}
        exit_point = 1.0
    return (
        orig_idx,
        entry_idx,
        exit_idx,
        1 if trade["side"] == "long_spread" else -1,
        open_at_end,
        float(trade["entry"]["spread_fill"]),
        float(trade["exit"]["spread_fill"]),
        float(entry_margin),
        *(float(entry_costs.get(key, 0.0)) for key in _COST_KEYS),
        *(float(exit_costs.get(key, 0.0)) for key in _COST_KEYS),
        float(exit_point),
    )


def _pack_trade_specs(
    trades: list[dict[str, Any]], date_to_idx: dict[str, int], terms: Terms
) -> tuple[list[dict[str, Any]], list[tuple[Any, ...]]] | None:
    intents = {t["entry"]["date"]: t for t in trades}
    intent_list: list[dict[str, Any]] = []
    trade_specs: list[tuple[Any, ...]] = []
    for entry_day, trade in intents.items():
        entry_idx = date_to_idx.get(entry_day)
        if entry_idx is None:
            continue
        exit_idx = date_to_idx.get(trade["exit"]["date"], -1)
        spec = _build_single_trade_spec(trade, len(intent_list), entry_idx, exit_idx, terms)
        if spec is None:
            return None
        intent_list.append(trade)
        trade_specs.append(spec)
    trade_specs.sort(key=lambda item: item[1])
    return intent_list, trade_specs


def _resolve_scalar_bounds(
    scalar_summary: tuple[str, str] | None, date_to_idx: dict[str, int], n_rows: int
) -> tuple[bool, int, int]:
    if scalar_summary is None:
        return False, 0, n_rows - 1
    s_i = date_to_idx.get(scalar_summary[0])
    e_i = date_to_idx.get(scalar_summary[1])
    if s_i is not None and e_i is not None and s_i <= e_i:
        return True, s_i, e_i
    return False, 0, n_rows - 1


def _hydrate_accepted_trades(
    intent_list: list[dict[str, Any]],
    acc_tuples: list[AcceptedTrade],
    marks: dict[str, float],
) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    for orig_idx, q, margin_rub, gross_rub, ec0, ec1, ec2, xc0, xc1, xc2, has_xc in acc_tuples:
        trade = intent_list[orig_idx]
        trade.update(
            number=len(accepted) + 1, quantity=q, margin_rub=margin_rub, gross_rub=gross_rub
        )
        trade["entry"]["costs"] = dict(zip(_COST_KEYS, (ec0, ec1, ec2), strict=True))
        trade["exit"]["costs"] = (
            dict(zip(_COST_KEYS, (xc0, xc1, xc2), strict=True)) if has_xc else {}
        )
        # Keep the C path's addition order rather than Python's compensated sum.
        hydrate_trade(trade, marks, ec0 + ec1 + ec2 + (xc0 + xc1 + xc2 if has_xc else 0.0))
        accepted.append(trade)
    return accepted


def size_proof_fast_c(
    fc: Any,
    proof: dict[str, Any],
    terms: Terms,
    marks: dict[str, float],
    initial: float,
    *,
    scan_cache_pair: tuple[dict[Any, tuple[Any, ...]] | None, Any] = (None, None),
    scalar_summary: tuple[str, str] | None = None,
) -> bool:
    rows = proof["series"]
    if not rows:
        return False
    scan_cache, scan_key = scan_cache_pair
    columns = _get_or_build_scan_columns(rows, terms, marks, scan_cache, scan_key)
    date_to_idx = columns[-1]
    packed = _pack_trade_specs(proof["trades"], date_to_idx, terms)
    if packed is None:
        return False
    intent_list, trade_specs = packed
    has_funding = any(
        r.get("funding_rub") is not None or r.get("funding_contracts", 0) for r in rows
    )
    use_scalar, start_idx, end_idx = _resolve_scalar_bounds(scalar_summary, date_to_idx, len(rows))
    ledger_or_summary, acc_tuples = fc.size_proof_metrics(
        *columns[:-1],
        rows,
        trade_specs,
        float(initial),
        not terms.settings.allow_failed_capital,
        terms.native_order,
        carry_settlement(proof),
        has_funding,
        use_scalar,
        start_idx,
        end_idx,
    )
    if ledger_or_summary == "overlap":
        raise W0Error("Перекрывающиеся сигналы не поддерживаются одним счётом окна.")
    if use_scalar:
        p0, end_price, end_wealth, end_cum_net, start_wealth, any_window_shortfall = (
            ledger_or_summary
        )
        exposed = any(e_i <= end_idx and (x_i < 0 or x_i >= start_idx) for e_i, x_i in acc_tuples)
        proof["_scalar_train_summary"] = (
            initial,
            p0,
            end_price,
            end_wealth,
            end_cum_net,
            start_wealth,
            bool(any_window_shortfall),
            exposed,
        )
    else:
        proof["trades"] = _hydrate_accepted_trades(intent_list, acc_tuples, marks)
        proof["ledger"] = ledger_or_summary
    return True
