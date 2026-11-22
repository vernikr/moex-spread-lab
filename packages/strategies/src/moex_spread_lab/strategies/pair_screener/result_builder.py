"""Result assembly for offline pair-screener evaluations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from moex_spread_lab.core.settings import EvaluationSplit
from moex_spread_lab.core.timeframes import TimeframeSpec


def mean_reversion_notes(spec: TimeframeSpec, has_funding: bool) -> list[str]:
    notes: list[str] = []
    if has_funding:
        notes.append(
            "Фандинг включён: биржевой SWAPRATE или явно отмеченная расчётная ставка; сумма = −position × SWAPRATE × лот × quantity."
        )
        notes.append(
            "Итоговый EOD SWAPRATE отражает клиринговый расчёт MOEX, включая действовавшие лимиты; минутный D/K1/K2 не пересчитывается. Лот берётся из текущего intake снапшота, не из эффективной исторической спецификации. Это не брокерская выписка."
        )
    elif spec.intraday:
        notes.append(
            "На выбранном внутридневном таймфрейме SWAPRATE не включён: дневную ставку нельзя размножать на внутридневные бары."
        )
    else:
        notes.append("Для пары без вечного фьючерса фандинг неприменим.")
    notes.append(
        "Сигнал по закрытию дня, исполнение по следующему открытию дня (прокси реакции в пределах минут–часов)."
        if not spec.intraday
        else f"Сигнал по закрытию бара {spec.label}; исполнение по открытию следующего бара (прокси)."
    )
    notes.append(
        "Открытая на конец выборки позиция отмечается как принудительный прокси-выход, а не как исполнимая сделка."
    )
    return notes


@dataclass
class StrategyRunOutcome:
    report: dict[str, Any]
    risk: dict[str, Any]
    value_key: str
    trades: int
    open_positions: int
    costs: Any
    provenance: dict[str, Any]
    price_role: str
    notes: list[str]
    evidence: Any


def assemble_screener_result(
    selection_meta: dict[str, Any],
    initial: float,
    through: str | None,
    share: float,
    outcome: StrategyRunOutcome,
) -> dict[str, Any]:
    provenance = dict(outcome.provenance)
    extra_notes = list(outcome.notes)
    if outcome.evidence:
        provenance.update(outcome.evidence.provenance)
        extra_notes.extend(outcome.evidence.notes())
    daily = [
        {
            "date": row["date"],
            "V": row[outcome.value_key],
            "B": row["benchmark_value"],
            "X": row["X"],
            "D": row["D"],
        }
        for row in outcome.report["daily"]
    ]
    first, last = daily[0], daily[-1]
    result: dict[str, Any] = {
        "selection": selection_meta,
        "status": "exploratory_proxy_not_live",
        "price_role": outcome.price_role,
        "period": {"from": first["date"], "to": last["date"], "observations": len(daily)},
        "metrics": {
            "strategy_return_pct": round((last["V"] / initial - 1) * 100, 3),
            "lqdt_return_pct": round((last["B"] / initial - 1) * 100, 3),
            "excess_rub": last["X"],
            "excess_pct_points": round(last["X"] / initial * 100, 3),
            "max_excess_drawdown_rub": round(max(row["D"] for row in daily), 2),
            "worst_lqdt_shortfall_rub": round(min(row["X"] for row in daily), 2),
            "closed_trades": outcome.trades,
            "open_positions": outcome.open_positions,
            "modeled_costs_rub": outcome.costs,
            "current_pair_margin_rub": outcome.risk["pair"]["pair_margin_no_offset_rub"],
            "stress_remaining_capital_rub": outcome.risk["summary"][
                "most_severe_remaining_capital_rub"
            ],
        },
        "daily": daily,
        "notes": [
            "Сопоставимый ценовой путь LQDT — не сверка с выпиской по счёту.",
            "Текущие снапшоты контрактов/маржи — запасные допущения, а не датированные условия брокера.",
            "Издержки: опубликованный тариф MOEX taker, нулевая брокерская комиссия VTB до 2026-12-31, slippage как параметр.",
            "Свободные средства держатся в LQDT, маржинальные средства ничего не зарабатывают (A-11); бенчмарк держит весь капитал в LQDT.",
            "Отбор по капиталу использует фиксированные рабочие буферы и текущие снапшоты маржи, а не потери, известные заранее.",
            *extra_notes,
        ],
        "provenance": provenance,
    }
    identity: dict[str, Any] = {
        "selection": result["selection"],
        "daily": daily,
        "source_hashes": {
            key: value for key, value in provenance.items() if "sha256" in key or "hashes" in key
        },
    }
    if outcome.evidence and outcome.evidence.provenance:
        identity["funding_evidence"] = outcome.evidence.provenance
    if through is None:
        split = EvaluationSplit.from_dates((r["date"] for r in daily), share)
        opening = (
            daily[len(split.training) - 1] if split.validation else {"V": initial, "B": initial}
        )
        strategy_change = last["V"] - opening["V"]
        benchmark_change = last["B"] - opening["B"]
        result["metrics_scope"] = "full_history"
        result["evaluation"] = {
            **split.metadata(),
            "result": {
                "strategy_change_rub": round(strategy_change, 2),
                "lqdt_change_rub": round(benchmark_change, 2),
                "excess_rub": round(strategy_change - benchmark_change, 2),
            },
        }
        identity["evaluation"] = result["evaluation"]
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result["run_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return result
