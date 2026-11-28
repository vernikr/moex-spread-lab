"""Causal, maximum-affordable sizing of proof signal intents on one finite account."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

from moex_spread_lab.core._fastcore_loader import load_fastcore
from moex_spread_lab.core.w0 import W0Error, run_id

from .costs import SHORTFALL_TOLERANCE, cost_totals
from .sizing_fast import (
    Terms,
    carry_settlement,
    hydrate_trade,
    maximum_quantity,
    positive,
    size_proof_fast_c,
)

AUTO_SIZING = "max_affordable"
SIZING_POLICY = "max-affordable.v1"


def automatic(request: dict[str, Any]) -> bool:
    return request.get("sizing") == AUTO_SIZING


class _Replay:
    def __init__(self, terms: Terms, initial: float, *, metrics_only: bool) -> None:
        self.terms = terms
        self.initial = self.free = self.wealth = initial
        self.posted = self.cumulative = self.peak = self.drawdown = 0.0
        self.paused = False
        self.shortfalls = 0
        self.metrics_only = metrics_only
        self.position: dict[str, Any] | None = None
        self.accepted: list[dict[str, Any]] = []
        self.rejected: list[dict[str, Any]] = []
        self.ledger: list[dict[str, Any]] = []

    def move(self, trade: dict[str, Any], end_mark: float, point: float) -> float:
        change = round(self.sign * (end_mark - self.mark) * point * trade["quantity"], 2)
        trade["_gross"] += change
        self.mark = end_mark
        return change

    def close(self, trade: dict[str, Any], *, at_open: bool) -> float:
        _, costs = self.terms.at_fill(trade["exit"], self.index, at_open=at_open)
        trade["exit"]["costs"] = (
            {}
            if trade.get("open_at_end") and self.carry
            else {key: round(value * trade["quantity"], 2) for key, value in costs.items()}
        )
        self.events.append(f"EXIT #{trade['number']}")
        self.position = None
        return sum(trade["exit"]["costs"].values())

    def exit(self) -> tuple[float, float, float, dict[str, Any] | None]:
        trade = self.position
        if trade is None:
            return 0.0, 0.0, self.wealth, None
        exiting = self.row["date"] == trade["exit"]["date"]
        should_exit = exiting and (self.exit_eligible or bool(trade.get("open_at_end")))
        if not should_exit and not (self.eligible and not exiting):
            return 0.0, 0.0, self.wealth, trade
        at_open = should_exit and self.terms.open_fill and not trade.get("open_at_end")
        point = (
            self.terms.fill_point(self.index, at_open=at_open)
            if should_exit
            else self.terms.point(self.index)
        )
        daily = self.move(
            trade, trade["exit"]["spread_fill"] if should_exit else self.row["spread"], point
        )
        if not should_exit:
            return daily, 0.0, self.wealth, trade
        cost = self.close(trade, at_open=at_open)
        owner = trade if self.row.get("funding_contracts", 0) else None
        return daily, cost, self.wealth + daily - cost, owner

    def skip(self, candidate: dict[str, Any], budget: float, margin: float) -> None:
        if self.metrics_only:
            return
        why = (
            "Недостаточно свободных средств для маржи; новые входы остановлены."
            if self.paused
            else "Недостаточно средств на один спред, издержки и обязательный резерв."
        )
        self.rejected.append(
            {
                "date": self.row["date"],
                "quantity": 0,
                "reason": why,
                "available_capital_rub": round(budget, 2),
                "margin_per_pair_rub": round(margin, 2),
                "signal": candidate.get("signal"),
            }
        )
        self.events.append("SKIP: " + why)

    def enter(
        self, candidate: dict[str, Any] | None, fill_wealth: float, owner: dict[str, Any] | None
    ) -> tuple[float, float, dict[str, Any] | None]:
        if candidate is None or not self.entry_eligible:
            return 0.0, 0.0, owner
        if self.position is not None:
            raise W0Error("Перекрывающиеся сигналы не поддерживаются одним счётом окна.")
        margin, costs = self.terms.at_fill(
            candidate["entry"], self.index, at_open=self.terms.open_fill
        )
        budget = max(0.0, min(self.initial, fill_wealth))
        qty = (
            0
            if self.paused
            else maximum_quantity(
                budget,
                margin,
                costs,
                stress=not self.terms.settings.allow_failed_capital,
                native_spread=self.terms.native_order,
            )
        )
        if not qty:
            self.skip(candidate, budget, margin)
            return 0.0, 0.0, owner
        candidate.update(
            number=len(self.accepted) + 1,
            quantity=qty,
            margin_rub=round(margin * qty, 2),
            _gross=0.0,
            _funding=0.0,
        )
        if not self.metrics_only:
            candidate["sizing"] = {
                "mode": AUTO_SIZING,
                "available_capital_rub": round(budget, 2),
                "margin_per_pair_rub": margin,
                "entry_cost_per_pair_rub": sum(costs.values()),
                "stress_reserve_enforced": not self.terms.settings.allow_failed_capital,
            }
            candidate["entry"]["margin_rub"] = candidate["margin_rub"]
        candidate["entry"]["costs"] = {key: round(value * qty, 2) for key, value in costs.items()}
        cost = sum(candidate["entry"]["costs"].values())
        self.accepted.append(candidate)
        self.events.append(f"ENTRY #{candidate['number']}")
        self.position = candidate
        self.sign = 1 if candidate["side"] == "long_spread" else -1
        self.mark = candidate["entry"]["spread_fill"]
        same_day = candidate["exit"]["date"] == self.row["date"]
        end_mark = candidate["exit"]["spread_fill"] if same_day else self.row["spread"]
        daily = (
            self.move(candidate, end_mark, self.terms.point(self.index)) if self.eligible else 0.0
        )
        if same_day:
            cost += self.close(candidate, at_open=False)
        return daily, cost, candidate

    def append_row(self, daily: float, cost: float, owner: dict[str, Any] | None) -> None:
        row, idx = self.row, self.index
        funding_q = (
            owner["quantity"] if owner is not None and row.get("funding_contracts", 0) else 0
        )
        funding = round(float(row.get("funding_rub") or 0.0) * funding_q, 2)
        if owner is not None:
            owner["_gross"] += funding
            owner["_funding"] += funding
        daily, cost = round(daily + funding, 2), round(cost, 2)
        self.cumulative = round(self.cumulative + daily - cost, 2)
        growth = self.prices[idx] / self.prices[idx - 1] if idx else 1.0
        grown = self.free * growth if self.free > 0 else self.free
        self.wealth = grown + self.posted + daily - cost
        if self.position is None:
            self.posted = 0.0
        elif self.eligible or self.entry_eligible:
            self.posted = self.terms.posted_margin(idx, self.position["quantity"])
        self.free = self.wealth - self.posted
        shortfall = self.free < -SHORTFALL_TOLERANCE
        self.shortfalls += int(shortfall)
        excess = self.wealth - self.initial * self.prices[idx] / self.prices[0]
        self.peak = max(self.peak, excess)
        self.drawdown = max(self.drawdown, self.peak - excess)
        self.paused |= shortfall
        ledger = {
            "date": row["date"],
            "cumulative_net_rub": self.cumulative,
            "account_value_rub": self.wealth,
            "cash_shortfall": shortfall,
        }
        self.ledger.append(ledger)
        if not self.metrics_only:
            self.full_row(ledger, daily, cost, funding, funding_q)

    def full_row(
        self, ledger: dict[str, Any], daily: float, cost: float, funding: float, funding_q: int
    ) -> None:
        row, day = self.row, self.row["date"]
        quantity = self.position["quantity"] if self.position is not None else 0
        payment = funding if row.get("funding_rub") is not None else None
        row.update(quantity=quantity, margin_rub=round(self.posted, 2), funding_rub=payment)
        if "funding_contracts" in row:
            row["funding_contracts"] = funding_q
        ledger.update(
            timestamp=row.get("timestamp", day),
            session_date=row.get("session_date", day[:10]),
            benchmark_mark_date=row.get("benchmark_mark_date"),
            position=self.sign if self.position else 0,
            quantity=quantity,
            mark_pnl_rub=daily,
            costs_rub=cost,
            funding_rub=payment,
            lqdt_on_margin_rub=None,
            event=" + ".join(self.events) or None,
            posted_margin_rub=self.posted,
            free_cash_rub=self.free,
        )

    def run(self, proof: dict[str, Any], marks: dict[str, float]) -> None:
        self.carry = carry_settlement(proof)
        self.prices = [positive(marks[r["date"]], "отметка LQDT") for r in self.terms.rows]
        intents = {t["entry"]["date"]: t for t in proof["trades"]}
        for index, row in enumerate(self.terms.rows):
            self.index, self.row = index, row
            self.eligible = bool(row.get("volume_eligible", True))
            self.entry_eligible = bool(row.get("entry_eligible", self.eligible))
            self.exit_eligible = bool(row.get("exit_eligible", self.eligible))
            self.events: list[str] = []
            daily_exit, cost_exit, fill_wealth, owner = self.exit()
            daily_entry, cost_entry, owner = self.enter(
                intents.get(self.row["date"]), fill_wealth, owner
            )
            self.append_row(daily_exit + daily_entry, cost_exit + cost_entry, owner)
        for trade in self.accepted:
            trade["gross_rub"] = round(trade.pop("_gross"), 2)
            funding = round(trade.pop("_funding"), 2)
            hydrate_trade(
                trade,
                marks,
                funding=None if self.metrics_only else funding,
                native=self.terms.native,
            )
        proof.update(trades=self.accepted, ledger=self.ledger)

    def report(self, proof: dict[str, Any], unit_id: str) -> None:
        terms, trades, initial = self.terms, self.accepted, self.initial
        settings = terms.settings
        selection = proof["selection"]
        selection.update(
            quantity=None, slippage_ticks=settings.slippage_ticks, initial_capital_rub=initial
        )
        strategy = selection["strategy"]
        if terms.native and strategy in {"zscore_mean_reversion", "convergence_to_ruonia"}:
            editable = (
                {"entry_z", "exit_z", "window"}
                if strategy == "zscore_mean_reversion"
                else {"threshold_pp"}
            )
            parameters = selection["parameters"]
            selection["strategy_conventions"] = {
                k: v for k, v in parameters.items() if k not in editable
            }
            selection["parameters"] = {k: v for k, v in parameters.items() if k in editable}
        proof["skipped_entries"] = self.rejected
        proof["sizing"].update(
            policy=SIZING_POLICY,
            mode=AUTO_SIZING,
            entry_capital_ceiling_rub=initial,
            stress_reserve_enforced=not settings.allow_failed_capital,
            exit_cost_reserve="entry_priced_one_side",
            uncertainty_buffer_basis="per_spread" if terms.native_order else "per_account",
            additional_funding_rub=0,
        )
        closed = [t for t in trades if not t.get("open_at_end")]
        gross, costs, hurdle = (
            round(sum(t[k] for t in trades), 2)
            for k in ("gross_rub", "costs_rub", "lqdt_on_margin_rub")
        )
        quantities = [t["quantity"] for t in trades]
        # Fixed-size template rounding disclosures do not describe the replayed account.
        for component in ("gross", "costs", "net"):
            proof["summary"].pop(f"displayed_trade_{component}_rounding_delta_rub", None)
        proof["summary"].update(
            trades=len(trades),
            closed_trades=len(closed),
            open_positions=len(trades) - len(closed),
            winners=sum(t["net_rub"] > 0 for t in closed),
            gross_rub=gross,
            costs_rub=costs,
            net_rub=round(gross - costs, 2),
            cost_totals=cost_totals(trades),
            lqdt_on_margin_rub=hurdle,
            excess_over_lqdt_rub=round(gross - costs - hurdle, 2),
            max_excess_drawdown_rub=round(self.drawdown, 2),
            ledger_reconciliation_rub=round(gross - costs - self.cumulative, 2),
            skipped_entries=len(self.rejected),
            min_quantity=min(quantities, default=0),
            max_quantity=max(quantities, default=0),
            cash_shortfall_rows=self.shortfalls,
        )
        if terms.native:
            proof["summary"]["fx_point_value_drift_rub"] = round(
                sum(t["gross_rub"] - t.get("gross_rub_static", t["gross_rub"]) for t in trades),
                2,
            )
        proof["notes"].extend(
            [
                f"Размер каждого входа — максимум целых спредов по последней доступной стоимости счёта, не выше исходного капитала {initial:,.2f} ₽ из общих настроек; размер удерживается до выхода. Прибыль не расширяет исходный лимит, пополнений нет.",
                "Размер покрывает ГО, комиссию и проскальзывание входа плюс резерв выхода по ценам входа. При обязательном стресс-гейте также учитываются 3×ГО, VM-буфер 5 000 ₽ на спред и буфер неопределённости 5 000 ₽ (для календаря — на спред, для пары — на счёт).",
                "Это максимум по модели капитала, не гарантия доступного объёма в стакане. Пропуски записаны отдельно; новая попытка — на следующем входном сигнале стратегии.",
            ]
        )
        proof["run_id"] = run_id(
            {
                "unit_intents": unit_id,
                "sizing": proof["sizing"],
                "settings": settings.metadata(),
                "trades": trades,
                "ledger": self.ledger,
                "skipped_entries": self.rejected,
            }
        )


def _require_executable_exits(proof: dict[str, Any]) -> None:
    """A planned exit must fall on a row the strategy can actually trade out of."""
    series = cast(list[dict[str, Any]], proof.get("series") or [])
    trades = cast(list[dict[str, Any]], proof.get("trades") or [])
    rows = {row["date"]: row for row in series}
    for trade in trades:
        if trade.get("open_at_end"):
            continue
        exit_date = trade["exit"]["date"]
        row = rows.get(exit_date)
        if row is None:
            raise W0Error(f"Плановая дата выхода {exit_date} отсутствует в окне расчёта.")
        eligible = bool(row.get("volume_eligible", True))
        if not bool(row.get("exit_eligible", eligible)):
            raise W0Error(
                f"Плановая дата выхода {exit_date} не исполнима (exit_eligible=False); "
                "перенесите выход на исполнимый день."
            )


def size_proof(
    unit: dict[str, Any],
    marks: dict[str, float],
    settings: Any,
    contracts: dict[str, Any],
    *,
    metrics_only: bool = False,
    copy_unit: bool = True,
    _scan_cache: dict[Any, Any] | None = None,
    _scan_key: Any = None,
    scalar_summary: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Replay independent signal intents; all account observations are prefix-causal."""
    initial = positive(settings.initial_capital_rub, "начальный капитал")
    proof = (
        {**unit, "selection": dict(unit["selection"])}
        if metrics_only
        else deepcopy(unit)
        if copy_unit
        else unit
    )
    _require_executable_exits(proof)
    terms = Terms(proof, contracts, settings)
    state = None
    fc = load_fastcore() if metrics_only else None
    if fc is None or not size_proof_fast_c(
        fc,
        proof,
        terms,
        marks,
        initial,
        scan_cache_pair=(_scan_cache, _scan_key),
        scalar_summary=scalar_summary,
    ):
        state = _Replay(terms, initial, metrics_only=metrics_only)
        state.run(proof, marks)
    proof["selection"]["sizing"] = AUTO_SIZING
    proof["initial_capital_rub"] = initial
    proof["sizing"] = {"initial_capital_rub": initial}
    if state is not None and not metrics_only:
        state.report(proof, unit["run_id"])
    return proof


__all__ = [
    "AUTO_SIZING",
    "SIZING_POLICY",
    "automatic",
    "maximum_quantity",
    "size_proof",
]
