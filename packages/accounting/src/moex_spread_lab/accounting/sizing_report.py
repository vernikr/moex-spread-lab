"""Report and summary assembly for causal Python position sizing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from moex_spread_lab.core.w0 import run_id as _run_id

from .costs import cost_totals as _cost_totals
from .sizing_terms import AUTO_SIZING, SIZING_POLICY
from .sizing_terms import Terms as _Terms
from .sizing_terms import money as _money


@dataclass
class ReplayState:
    initial: float
    free: float
    wealth: float
    posted: float = 0.0
    cumulative: float = 0.0
    peak: float = 0.0
    drawdown: float = 0.0
    paused: bool = False
    shortfalls: int = 0
    position: dict[str, Any] | None = None
    accepted: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    ledger: list[dict[str, Any]] = field(default_factory=list)


def _finalize_metrics_only(
    proof: dict[str, Any], state: ReplayState, marks: dict[str, float]
) -> dict[str, Any]:
    for trade in state.accepted:
        trade["gross_rub"] = _money(trade.pop("_gross"))
        trade.pop("_funding")
        trade["costs_rub"] = _money(
            sum(sum(trade[side]["costs"].values()) for side in ("entry", "exit"))
        )
        trade["net_rub"] = _money(trade["gross_rub"] - trade["costs_rub"])
        trade["lqdt_on_margin_rub"] = _money(
            trade["margin_rub"] * (marks[trade["exit"]["date"]] / marks[trade["entry"]["date"]] - 1)
        )
    proof["selection"]["sizing"] = AUTO_SIZING
    proof["initial_capital_rub"] = state.initial
    proof["trades"] = state.accepted
    proof["ledger"] = state.ledger
    proof["sizing"] = {"initial_capital_rub": state.initial}
    return proof


def _finalize_full_trades(
    accepted: list[dict[str, Any]], marks: dict[str, float], *, native: bool
) -> None:
    for trade in accepted:
        trade["gross_rub"] = _money(trade.pop("_gross"))
        total_funding = _money(trade.pop("_funding"))
        if trade.get("funding_rub") is not None or not native:
            trade["funding_rub"] = total_funding
        trade["costs_rub"] = _money(
            sum(sum(trade[side]["costs"].values()) for side in ("entry", "exit"))
        )
        trade["net_rub"] = _money(trade["gross_rub"] - trade["costs_rub"])
        trade["lqdt_on_margin_rub"] = _money(
            trade["margin_rub"] * (marks[trade["exit"]["date"]] / marks[trade["entry"]["date"]] - 1)
        )
        trade["excess_rub"] = _money(trade["net_rub"] - trade["lqdt_on_margin_rub"])
        trade["result"] = (
            "win" if trade["net_rub"] > 0 else "loss" if trade["net_rub"] < 0 else "flat"
        )
        if "gross_rub_static" in trade:
            trade["gross_rub_static"] = _money(trade["gross_rub_static"] * trade["quantity"])


def _update_native_conventions(selection: dict[str, Any], *, native: bool) -> None:
    strategy = selection["strategy"]
    if not native or strategy not in {"zscore_mean_reversion", "convergence_to_ruonia"}:
        return
    editable = (
        {"entry_z", "exit_z", "window"} if strategy == "zscore_mean_reversion" else {"threshold_pp"}
    )
    parameters = selection["parameters"]
    selection["strategy_conventions"] = {k: v for k, v in parameters.items() if k not in editable}
    selection["parameters"] = {k: v for k, v in parameters.items() if k in editable}


def finalize_sized_proof(
    unit: dict[str, Any],
    proof: dict[str, Any],
    state: ReplayState,
    marks: dict[str, float],
    settings: Any,
    terms: _Terms,
    *,
    metrics_only: bool,
) -> dict[str, Any]:
    if metrics_only:
        return _finalize_metrics_only(proof, state, marks)

    _finalize_full_trades(state.accepted, marks, native=terms.native)
    closed = [t for t in state.accepted if not t.get("open_at_end")]
    gross = _money(sum(t["gross_rub"] for t in state.accepted))
    costs = _money(sum(t["costs_rub"] for t in state.accepted))
    hurdle = _money(sum(t["lqdt_on_margin_rub"] for t in state.accepted))
    quantities = [t["quantity"] for t in state.accepted]
    initial = state.initial
    proof["selection"].update(
        sizing=AUTO_SIZING,
        quantity=None,
        slippage_ticks=settings.slippage_ticks,
        initial_capital_rub=initial,
    )
    _update_native_conventions(proof["selection"], native=terms.native)
    proof.update(
        initial_capital_rub=initial,
        trades=state.accepted,
        ledger=state.ledger,
        skipped_entries=state.rejected,
        sizing={
            "policy": SIZING_POLICY,
            "mode": AUTO_SIZING,
            "initial_capital_rub": initial,
            "entry_capital_ceiling_rub": initial,
            "stress_reserve_enforced": not settings.allow_failed_capital,
            "exit_cost_reserve": "entry_priced_one_side",
            "uncertainty_buffer_basis": "per_spread" if terms.native_order else "per_account",
            "additional_funding_rub": 0,
        },
    )
    # A fixed-size template may contain rounding disclosures from its own trades;
    # those are not applicable to the independently replayed sized account.
    for component in ("gross", "costs", "net"):
        proof["summary"].pop(f"displayed_trade_{component}_rounding_delta_rub", None)
    proof["summary"].update(
        trades=len(state.accepted),
        closed_trades=len(closed),
        open_positions=len(state.accepted) - len(closed),
        winners=sum(t["net_rub"] > 0 for t in closed),
        gross_rub=gross,
        costs_rub=costs,
        net_rub=_money(gross - costs),
        cost_totals=_cost_totals(state.accepted),
        lqdt_on_margin_rub=hurdle,
        excess_over_lqdt_rub=_money(gross - costs - hurdle),
        max_excess_drawdown_rub=_money(state.drawdown),
        ledger_reconciliation_rub=_money(gross - costs - state.cumulative),
        skipped_entries=len(state.rejected),
        min_quantity=min(quantities, default=0),
        max_quantity=max(quantities, default=0),
        cash_shortfall_rows=state.shortfalls,
    )
    if terms.native:
        proof["summary"]["fx_point_value_drift_rub"] = _money(
            sum(t["gross_rub"] - t.get("gross_rub_static", t["gross_rub"]) for t in state.accepted)
        )
    proof["notes"].extend(
        [
            f"Размер каждого входа — максимум целых спредов по последней доступной стоимости счёта, не выше исходного капитала {initial:,.2f} ₽ из общих настроек; размер удерживается до выхода. Прибыль не расширяет исходный лимит, пополнений нет.",
            "Размер покрывает ГО, комиссию и проскальзывание входа плюс резерв выхода по ценам входа. При обязательном стресс-гейте также учитываются 3×ГО, VM-буфер 5 000 ₽ на спред и буфер неопределённости 5 000 ₽ (для календаря — на спред, для пары — на счёт).",
            "Это максимум по модели капитала, не гарантия доступного объёма в стакане. Пропуски записаны отдельно; новая попытка — на следующем входном сигнале стратегии.",
        ]
    )
    proof["run_id"] = _run_id(
        {
            "unit_intents": unit["run_id"],
            "sizing": proof["sizing"],
            "settings": settings.metadata(),
            "trades": state.accepted,
            "ledger": state.ledger,
            "skipped_entries": state.rejected,
        }
    )
    return proof
