"""Assemble the `execution-proof.v1` payload shared by both pair strategies.

The pair strategies and the funding engine fill the shared structs; the retired S2
`screener_search` shim that used to enrich the mean-reversion payload was deleted on
2026-11-26 (issue #243).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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
from moex_spread_lab.strategies import REFERENCE_WARNING, attach_entry_levels

from .ledger_blocks import apply_raw_trade_totals
from .market_window import PairMarketWindow
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


CARRY_FILL_NOTE = (
    "Фандинг удерживаемой позиции = −position × SWAPRATE × лот × число контрактов. Источник ставки — "
    "итог клиринга MOEX ISS, кроме явно отмеченных оценок. Вход/выход по open — прокси времени "
    "исполнения; лот — из текущего снапшота. Это не пересчёт минутных D/K1/K2 и не брокерская выписка."
)
VOLUME_RULE_NOTE = (
    "Дни, когда объём любой ноги равен нулю или отсутствует, остаются в журнале, но исключены из "
    "индикаторов, гейта объёма и симуляции сделок; позиции на этих датах не переоцениваются."
)
NATIVE_OPEN_POSITION_NOTE = (
    "Открытая на конец выборки позиция отмечается как принудительный прокси-выход, а не как "
    "исполнимая сделка."
)
SPREAD_NOTES = (
    "Спред пары — близкая минус дальняя, нормированный в рублях, а не собственный инструмент; его "
    "объём не наблюдаем.",
    "Спред показан в % от стоимости дальней ноги; абсолютные отметки в рублях — в CSV и во второй "
    "строке таблицы.",
)


def notes(window: PairMarketWindow, trades: list[dict[str, Any]]) -> list[str]:
    """Qualify the proxy price path, funding proxies and any forced end-of-sample exit."""
    intraday = get_timeframe(window.series["timeframe"]).intraday
    evidence = window.evidence
    if evidence is None:
        funding_notes = [
            "На выбранном внутридневном таймфрейме SWAPRATE не включён: дневную ставку нельзя "
            "размножать на внутридневные бары."
            if intraday
            else "Для пары без вечного фьючерса фандинг неприменим."
        ]
    else:
        funding_notes = [*evidence.notes(), CARRY_FILL_NOTE]
    fill_note = {
        "reference_same_bar": REFERENCE_WARNING,
        "next_open": (
            "Сигнал по закрытию дня; исполнение по следующему открытию дня (прокси открытия candle); "
            "отметки по закрытию."
        ),
    }.get(
        window.spec.execution["fill"],
        "Сигнал по закрытию дня; исполнение по закрытию следующего дня (ценовой прокси); отметки по "
        "закрытию.",
    )
    return [
        *funding_notes,
        VOLUME_RULE_NOTE,
        fill_note,
        *([NATIVE_OPEN_POSITION_NOTE] if any(trade["open_at_end"] for trade in trades) else []),
        *SPREAD_NOTES,
    ]


def full_payload(
    window: PairMarketWindow,
    accounted: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], float],
    raw_totals: tuple[float, float, float],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Full `execution-proof.v1` payload with chart series, notes and the built-in run id."""
    rows, trades, ledger, drawdown = accounted
    spec = window.spec
    return pair_payload(
        PairPayloadSpec(
            pair=spec.pair,
            strategy="mean_reversion",
            fill_label=spec.execution["fill"],
            quantity=int(spec.execution["quantity"]),
            slippage=float(spec.execution["slippage_ticks"]),
            params=spec.params,
            terms=spec.terms,
            series_rows=rows,
            trades=trades,
            ledger=ledger,
            daily_rows=[{"D": drawdown}],
            margin_pair=window.margin_pair,
            provenance=provenance,
            notes=notes(window, trades),
            risk=spec.risk,
            raw_trade_totals=raw_totals,
        )
    )
