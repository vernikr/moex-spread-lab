"""Assemble the `execution-proof.v1` payload shared by both pair strategies.

The pair strategies and the funding engine fill the shared structs; the retired S2
`screener_search` shim that used to enrich the mean-reversion payload was deleted on
2026-11-26 (issue #243).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from math import fsum
from typing import Any

from moex_spread_lab.accounting.costs import (
    CONTRACT_GROUP,
    TAKER_RATE_PERCENT,
)
from moex_spread_lab.accounting.costs import (
    cost_totals as _cost_totals,
)
from moex_spread_lab.core.settings import MIN_DAILY_VOLUME
from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import run_id
from moex_spread_lab.strategies import attach_entry_levels

from .payload_tools import PROOF_SCHEMA, clean, observed_min_leg_volume, point_value


@dataclass(frozen=True, slots=True)
class PairPayloadSpec:
    """Inputs of one `execution-proof.v1` pair payload."""

    pair: dict[str, str]
    strategy: str
    fill_label: str
    quantity: int
    slippage: float
    params: dict[str, Any]
    terms: dict[str, dict[str, Any]]
    series_rows: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    ledger: list[dict[str, Any]]
    daily_rows: list[dict[str, Any]]
    margin_pair: float
    provenance: dict[str, Any]
    notes: list[str]
    risk: dict[str, Any]
    raw_trade_totals: tuple[float, float, float] | None = None


def _price_role(spec: PairPayloadSpec, timeframe: str) -> str:
    """Human-readable description of what the prices in the payload are."""
    if spec.strategy != "mean_reversion":
        return "ISS: расчётная цена на конец дня (прокси)"
    label = get_timeframe(timeframe).label
    return {
        "reference_same_bar": (
            f"ISS: свеча {label}, справочное закрытие сигнального бара, не исполнимая доходность"
        ),
        "next_open": f"ISS: свеча {label}, заполнение по открытию, отметки по закрытию",
    }.get(
        spec.fill_label,
        f"ISS: свеча {label}, следующее закрытие (прокси)",
    )


def _instrument_block(spec: PairPayloadSpec, near: str, far: str) -> dict[str, Any]:
    return {
        "near": near,
        "far": far,
        "family": spec.pair["family"],
        "near_expiry": spec.terms[near].get("expiry"),
        "far_expiry": spec.terms[far].get("expiry"),
        "near_point_value_rub": point_value(spec.terms[near]) if spec.terms[near] else None,
        "far_point_value_rub": point_value(spec.terms[far]) if spec.terms[far] else None,
        "far_contract_historical_evidence": spec.terms[far].get("historical_evidence"),
        "far_contract_specification_basis": spec.terms[far].get("historical_specification_basis"),
        "spread_instrument": None,
    }


def _window_block(spec: PairPayloadSpec, near: str, far: str, timeframe: str) -> dict[str, Any]:
    min_leg_volume = observed_min_leg_volume(spec.series_rows)
    return {
        "from": spec.series_rows[0]["date"] if spec.series_rows else None,
        "to": spec.series_rows[-1]["date"] if spec.series_rows else None,
        "timeframe": timeframe,
        "observations": len(spec.series_rows),
        "near": near,
        "far": far,
        "min_spread_volume": None,
        "min_leg_volume": min_leg_volume,
        "tradable_every_day": bool(
            min_leg_volume is not None and min_leg_volume >= MIN_DAILY_VOLUME
        ),
        "min_daily_volume_rule": MIN_DAILY_VOLUME,
        "excluded_volume_observations": sum(
            not row.get("volume_eligible", True) for row in spec.series_rows
        ),
        "passes_capital_screen": spec.risk["summary"]["all_scenarios_passed"],
        "max_pair_margin_rub": spec.risk["pair"]["pair_margin_no_offset_rub"],
    }


def _contract_block(spec: PairPayloadSpec, near: str, far: str) -> dict[str, Any]:
    near_group = CONTRACT_GROUP.get(near)
    common_group = near_group if near_group == CONTRACT_GROUP.get(far) else "mixed"
    terms = spec.terms[near]
    return {
        "point_value_rub": point_value(terms) if terms else None,
        "tick_size": terms.get("tick_size") if terms else None,
        "tariff_group": common_group,
        "taker_rate_percent": TAKER_RATE_PERCENT.get(common_group)
        if common_group is not None and common_group != "mixed"
        else None,
        "broker_fee_rub": 0.0,
    }


def _summary_block(
    spec: PairPayloadSpec, closed: list[dict[str, Any]], gross: float, costs: float, net: float
) -> dict[str, Any]:
    hurdle = sum(trade["lqdt_on_margin_rub"] for trade in spec.trades)
    cumulative = spec.ledger[-1]["cumulative_net_rub"] if spec.ledger else 0.0
    return {
        "trades": len(spec.trades),
        "closed_trades": len(closed),
        "open_positions": len(spec.trades) - len(closed),
        "winners": sum(trade["net_rub"] > 0 for trade in closed),
        "gross_rub": round(gross, 2),
        "costs_rub": round(costs, 2),
        "net_rub": round(net, 2),
        "cost_totals": _cost_totals(spec.trades),
        "lqdt_on_margin_rub": round(hurdle, 2),
        "excess_over_lqdt_rub": round(net - hurdle, 2),
        "max_excess_drawdown_rub": round(
            max((float(row.get("D") or 0.0) for row in spec.daily_rows), default=0.0), 2
        ),
        "ledger_reconciliation_rub": round(net - cumulative, 2),
        "fx_point_value_drift_rub": None,
    }


def _trade_volume_rule(spec: PairPayloadSpec) -> bool | None:
    """`None` without trades: there is nothing to audit."""
    if not spec.trades:
        return None
    return all(
        min(trade[side].get("near_volume") or 0, trade[side].get("far_volume") or 0) > 0
        for trade in spec.trades
        for side in ("entry", "exit")
    )


def _notes_block(spec: PairPayloadSpec, far: str) -> list[str]:
    fallback = (
        [f"Исторические параметры дальней ноги используют fallback: {basis}."]
        if (basis := spec.terms[far].get("historical_specification_basis"))
        else []
    )
    rule = _trade_volume_rule(spec)
    rule_notes = (
        [f"Проверка правила объёма в дни сделок: {'пройдена' if rule else 'нарушена'}."]
        if rule is not None
        else []
    )
    return [
        "Сопоставимый ценовой путь LQDT — не сверка с выпиской по счёту.",
        "Текущие снапшоты контрактов/маржи — запасные допущения, а не датированные условия брокера.",
        *fallback,
        "Свободные средства держатся в LQDT, маржинальные средства ничего не зарабатывают (A-11); бенчмарк держит весь капитал в LQDT.",
        "Цены — candle/расчётные прокси, не исполнимые заполнения.",
        "Издержки: опубликованный тариф MOEX taker, нулевая брокерская комиссия VTB до 2026-12-31, проскальзывание как параметр.",
        "Отбор по капиталу использует фиксированные рабочие буферы и текущие снапшоты маржи, а не потери, известные заранее.",
        *spec.notes,
        *rule_notes,
    ]


def _run_identity(spec: PairPayloadSpec, payload: dict[str, Any]) -> str:
    funding_evidence = (
        {
            "funding_evidence": {
                key: value for key, value in spec.provenance.items() if key.startswith("funding_")
            }
        }
        if "funding_policy" in spec.provenance
        else {}
    )
    return run_id(
        {
            "selection": payload["selection"],
            "daily": payload["series"],
            **funding_evidence,
            "source_hashes": {
                key: value
                for key, value in spec.provenance.items()
                if isinstance(value, str) and "sha256" in key
            },
        }
    )


def pair_payload(spec: PairPayloadSpec) -> dict[str, Any]:
    pair_id, near, far = spec.pair["pair_id"], spec.pair["near"], spec.pair["far"]
    closed = [trade for trade in spec.trades if not trade["open_at_end"]]
    gross = sum(trade["gross_rub"] for trade in spec.trades)
    costs = sum(trade["costs_rub"] for trade in spec.trades)
    net = sum(trade["net_rub"] for trade in spec.trades)
    timeframe = spec.series_rows[0].get("timeframe", "1d") if spec.series_rows else "1d"
    selection_out = {
        "universe": PAIR_UNIVERSE,
        "pair": pair_id,
        "strategy": spec.strategy,
        "timeframe": timeframe,
        "fill": spec.fill_label,
        "quantity": spec.quantity,
        "slippage_ticks": spec.slippage,
        "parameters": spec.params,
        "initial_capital_rub": spec.risk["stress_inputs"]["initial_capital_rub"],
    }
    payload: dict[str, Any] = {
        "schema_version": PROOF_SCHEMA,
        "universe": PAIR_UNIVERSE,
        "status": "exploratory_proxy_not_live",
        "price_role": _price_role(spec, timeframe),
        "selection": selection_out,
        "initial_capital_rub": selection_out["initial_capital_rub"],
        "instrument": _instrument_block(spec, near, far),
        "window": _window_block(spec, near, far, timeframe),
        "contract": _contract_block(spec, near, far),
        "series": spec.series_rows,
        "trades": spec.trades,
        "ledger": spec.ledger,
        "summary": _summary_block(spec, closed, gross, costs, net),
        "notes": _notes_block(spec, far),
        "provenance": spec.provenance,
    }
    if spec.raw_trade_totals is not None:
        apply_raw_trade_totals(payload["summary"], spec.trades, spec.ledger, spec.raw_trade_totals)
    attach_entry_levels(payload)
    payload = clean(payload)
    payload["run_id"] = _run_identity(spec, payload)
    return payload


def apply_raw_trade_totals(
    summary: dict[str, Any],
    trades: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    raw_totals: Sequence[float],
) -> None:
    """Update summary only; do not mutate the ledger, trade rows or account curve."""
    gross, costs, net = raw_totals
    for name, value in (("gross", gross), ("costs", costs), ("net", net)):
        key = f"{name}_rub"
        rounded = round(value, 2)
        displayed_sum = round(fsum(float(t[key]) for t in trades), 2)
        delta = round(displayed_sum - rounded, 2)
        summary[key] = rounded
        delta_key = f"displayed_trade_{name}_rounding_delta_rub"
        if delta:
            summary[delta_key] = delta
        else:
            summary.pop(delta_key, None)
    hurdle = fsum(float(t["lqdt_on_margin_rub"]) for t in trades)
    summary["excess_over_lqdt_rub"] = round(net - hurdle, 2)
    cumulative = ledger[-1]["cumulative_net_rub"] if ledger else 0.0
    summary["ledger_reconciliation_rub"] = round(round(net, 2) - cumulative, 2)


OPEN_POSITION_NOTE = (
    "Открытая на конец выборки позиция — принудительный прокси-выход, не исполнимая сделка."
)
OPEN_POSITION_PREFIX = OPEN_POSITION_NOTE[:40]
PLUGIN_SOURCE_NOTE = "Решения — из проверенных намерений плагина; цены и учёт рассчитаны отдельно."


def merge_pair_series_row(
    template_row: dict[str, Any], actual_row: dict[str, Any], *, builtin: bool
) -> dict[str, Any]:
    """Merge plugin accounting into one native series row.

    A third-party plugin supplies indicator values of its own, so ``signal_z`` is cleared and
    its extra columns are kept. A built-in plugin runs the same engine as the native proof, so
    its row must stay byte-identical to the native one: native columns win and plugin-only
    columns are dropped.
    """
    if builtin:
        row = {key: actual_row.get(key, value) for key, value in template_row.items()}
        row["signal_z"] = template_row.get("signal_z")
        return row
    return {**template_row, **actual_row, "signal_z": None}


def apply_pair_notes(template: dict[str, Any], trades: list[dict[str, Any]]) -> None:
    """Attach drop-in provenance notes; built-in payloads keep their native wording."""
    template["notes"] = [
        note for note in template["notes"] if not note.startswith(OPEN_POSITION_PREFIX)
    ]
    if any(trade["open_at_end"] for trade in trades):
        template["notes"].append(OPEN_POSITION_NOTE)
    template["notes"].append(PLUGIN_SOURCE_NOTE)


TRIALS_PER_STRATEGY = 80
UI_TRIALS_ENV = "MOEX_UI_SMOKE_TRIALS"


def budget() -> int:
    """Trials per strategy family for this process: 80, or an offline smoke's bound."""
    if os.environ.get("MOEX_OFFLINE_ONLY") != "1":
        return TRIALS_PER_STRATEGY
    raw = os.environ.get(UI_TRIALS_ENV, "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        return TRIALS_PER_STRATEGY
    return min(int(raw), TRIALS_PER_STRATEGY)
