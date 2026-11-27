"""`execution-proof.v1` payloads of the carry replay.

The sizing pass consumes the price-only intent payload (`carry_route`), the tuning
search the metrics payload and the web UI the full settlement-priced payload.
"""

from __future__ import annotations

from typing import Any

from moex_spread_lab.core.contracts import plugin_trust_tier

from .carry_route import (
    PERPETUAL_OPEN_STATUS,
    CarryWindowState,
    carry_plugin_of,
    funding_head,
    reference_costs,
    report_for,
    sizing_intent_payload,
    strategy_name_of,
    study_cut,
)
from .ledger_blocks import funding_ledger
from .market_window import ReplaySpec
from .pair_payload import SPREAD_NOTES, PairPayloadSpec, pair_payload
from .payload_tools import (
    TradeSpec,
    fill_block,
    rel_pct,
    trade_block,
)

CARRY_PLUGIN_NOTE = (
    "Сигналы предоставлены доверенным carry-плагином: их причинность должна проверяться отдельно. "
    "Исполнение — расчёт в тот же день; обычный выход теряет фондирование этого дня."
)
BUILTIN_SIGNAL_NOTE = (
    "Сигнал встроенной стратегии использует фондирование до предыдущего клиринга; прокси исполнения — "
    "расчёт в тот же день; выход в тот же день теряет фондирование этого дня."
)
SWAPRATE_NOTE = (
    "В таблице SWAPRATE показан отдельно от денежного итога: начисление/списание = SWAPRATE × лот × "
    "число удерживаемых контрактов; положительный итог здесь означает получение шортом ближнего "
    "перпетуала. Базовый источник — итог клиринга ISS; оценки помечены отдельно. Это не брокерская "
    "выписка."
)
OPEN_POSITION_NOTE = (
    "Открытая на конец выборки позиция отмечается как принудительный прокси-выход, а не как "
    "исполняемая сделка."
)


def _settlement_fill(
    state: CarryWindowState,
    daily_by_date: dict[str, dict[str, Any]],
    day: str,
    costs: dict[str, float],
) -> dict[str, Any]:
    """Fill block from the settlement prices of one journal day."""
    row = daily_by_date[day]
    near_price, far_price = row["near_settle"], row["far_settle"]
    far_value = far_price * state.far_mult
    return fill_block(
        day,
        round(near_price * state.near_mult - far_value, 8),
        near_price,
        far_price,
        daily_by_date,
        costs,
        far_value=far_value,
    )


def _entry_costs(state: CarryWindowState) -> dict[str, float]:
    """Round-trip reference side cost scaled to the requested quantity."""
    quantity = int(state.spec.execution["quantity"])
    return {key: round(value * quantity, 2) for key, value in reference_costs(state).items()}


def _trade_rows(state: CarryWindowState, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Settlement-priced trade rows for the full payload."""
    spec = state.spec
    costs = _entry_costs(state)
    daily_by_date = {row["date"]: row for row in report["daily"]}
    last_date = report["daily"][-1]["date"]
    reasons = {(sig["date"], sig["action"]): sig.get("reason") for sig in report.get("signals", [])}
    trades: list[dict[str, Any]] = []
    for number, row in enumerate(report["trades"], start=1):
        open_at_end = row["status"] == PERPETUAL_OPEN_STATUS
        entry_date = row["entry_execution_date"]
        exit_date = row["exit_execution_date"] or last_date
        trades.append(
            trade_block(
                TradeSpec(
                    number=number,
                    side="short_spread",
                    quantity=int(spec.execution["quantity"]),
                    signal={
                        "kind": "funding",
                        "value": None,
                        "date": row["entry_decision_date"],
                        "reason": reasons.get((row["entry_decision_date"], "entry")),
                    },
                    entry=_settlement_fill(state, daily_by_date, entry_date, costs),
                    exit=_settlement_fill(
                        state, daily_by_date, exit_date, {} if open_at_end else costs
                    ),
                    exit_reason=(
                        row.get("reason")
                        or reasons.get((row.get("exit_decision_date") or "", "exit"))
                        or row["status"]
                    ),
                    margin_pair=state.margin_pair,
                    gross_rub=float(row["gross_pnl"]),
                    costs_rub=float(row["transaction_costs"]),
                    net_rub=float(row["net_pnl"]),
                    open_at_end=open_at_end,
                ),
                benchmark_dates=spec.benchmark_dates or [],
                benchmark_prices=spec.benchmark_prices or {},
            )
        )
    return trades


def _series_rows(state: CarryWindowState, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Daily settlement rows with funding, swaprate and margin columns."""
    spec = state.spec
    quantity = int(spec.execution["quantity"])
    near = spec.pair["near"]
    near_lot = float(spec.terms[near].get("lot_size") or 1.0)
    marks = state.evidence.marks
    book_marks = state.funding_window.book.marks
    rows: list[dict[str, Any]] = []
    for row in report["daily"]:
        position = 1 if row["position"] else 0
        study_row = state.study_by_date.get(row["date"], {})
        spread = row["near_settle"] * state.near_mult - row["far_settle"] * state.far_mult
        rows.append(
            {
                "date": row["date"],
                "near": row["near_settle"],
                "far": row["far_settle"],
                "near_open": None,
                "far_open": None,
                "spread": round(spread, 8),
                "spread_open": None,
                "spread_last_trade": None,
                "spread_rel": rel_pct(spread, row["far_settle"] * state.far_mult),
                "spread_open_rel": None,
                "spread_last_trade_rel": None,
                "near_volume": row.get("near_volume"),
                "far_volume": row.get("far_volume"),
                "spread_volume": None,
                "spread_trades": None,
                "days_to_near_expiry": None,
                "funding_rub": round(row["daily_funding_included_rub"], 2),
                "funding_swaprate_rub_per_unit": study_row.get("near_swaprate"),
                "funding_lot_size": near_lot,
                "funding_contracts": quantity if position else 0,
                "funding_source": "MOEX ISS SWAPRATE; EOD settlement history",
                **(marks[row["date"]].series_fields(book_marks) if row["date"] in marks else {}),
                "signal_z": None,
                "margin_rub": round(state.margin_pair, 2) if position else 0.0,
            }
        )
    return rows


def _notes(state: CarryWindowState, trades: list[dict[str, Any]]) -> list[str]:
    """Provenance notes for the funding proxy, plus pair-specific caveats."""
    spec, evidence = state.spec, state.evidence
    near, pair_id = spec.pair["near"], spec.pair["pair_id"]
    plugin = carry_plugin_of(spec)
    return [
        *evidence.notes(),
        BUILTIN_SIGNAL_NOTE
        if plugin is None or plugin_trust_tier(plugin) == "builtin"
        else CARRY_PLUGIN_NOTE,
        SWAPRATE_NOTE,
        *(
            [
                "Для GLDRUBF MOEX публикует K1=0,05% и K2=0,15%; источник исходных ставок — ISS. "
                "Внутридневное отклонение D и параметры режима из исторических данных не "
                "реконструируются."
            ]
            if near == "GLDRUBF"
            else []
        ),
        *(
            ["Потолки правил фондирования для не-CNY инструментов не восстанавливаются."]
            if pair_id != "CNYRUBF/CRZ6"
            else []
        ),
        *SPREAD_NOTES,
        (
            "Комиссии и проскальзывание пересчитываются по прокси-ценам каждого входа и выхода; "
            "проскальзывание — из общих настроек."
            if spec.execution.get("sizing_intents")
            else "Комиссии — опубликованный тариф MOEX taker, смоделированный на текущем референсе "
            "(расчёт по последней строке), а не на каждом ценовом значении исполнения."
        ),
        *([OPEN_POSITION_NOTE] if any(trade["open_at_end"] for trade in trades) else []),
    ]


def _metrics_payload(
    state: CarryWindowState,
    trades: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    strategy_name: str,
) -> dict[str, Any]:
    """Tuning-search shape: trades and ledger only."""
    return {
        **funding_head(state, strategy_name),
        "trades": trades,
        "ledger": ledger,
        "provenance": state.evidence.provenance,
    }


def run_carry(spec: ReplaySpec, *, metrics_only: bool = False) -> dict[str, Any]:
    """Run the funding/carry replay for one prepared window and cut-off."""
    state = study_cut(spec)
    strategy_name = strategy_name_of(spec)
    report = report_for(state, metrics_only=metrics_only)
    quantity = int(spec.execution["quantity"])
    if metrics_only and spec.execution.get("sizing_intents"):
        return sizing_intent_payload(state, report, strategy_name)
    trades = _trade_rows(state, report)
    if metrics_only:
        ledger = funding_ledger(report["daily"], trades)
        return _metrics_payload(state, trades, ledger, strategy_name)
    series_rows = _series_rows(state, report)
    ledger = funding_ledger(report["daily"], trades)
    _, near_source, far_source, _, source_hashes = spec.prepared
    return pair_payload(
        PairPayloadSpec(
            pair=spec.pair,
            strategy=strategy_name,
            fill_label="same_day_settlement",
            quantity=quantity,
            slippage=float(spec.execution["slippage_ticks"]),
            params=spec.params,
            terms=spec.terms,
            series_rows=series_rows,
            trades=trades,
            ledger=ledger,
            daily_rows=report["daily"],
            margin_pair=state.margin_pair,
            provenance={
                "near_history_sha256": near_source["raw_response_sha256"],
                "far_history_sha256": far_source["raw_response_sha256"],
                **(source_hashes or {}),
                **state.evidence.provenance,
            },
            notes=_notes(state, trades),
            risk=spec.risk,
        )
    )
