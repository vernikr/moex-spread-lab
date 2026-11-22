"""Fixed-quantity pair-plugin accounting.

Recomputes fills, cash flows and the journal from the plugin's unit intents.
"""

from __future__ import annotations

import math
from typing import Any

from moex_spread_lab.accounting.costs import _cost_totals, side_cost, tariff_profile
from moex_spread_lab.accounting.portfolio import lqdt_overlay_values
from moex_spread_lab.core.w0 import W0Error, _run_id
from moex_spread_lab.strategies import attach_entry_levels

from .fixed_plugin_series import _scaled_series
from .ledger_blocks import price_ledger
from .payload_tools import TradeSpec, fill_block, mark, point_value, trade_block
from .plugin_notes import (
    OPEN_POSITION_NOTE,
    OPEN_POSITION_PREFIX,
    PLUGIN_SOURCE_NOTE,
)
from .rounding_totals import apply_raw_trade_totals


def _trade_blocks(
    unit: dict[str, Any],
    rows_by_date: dict[str, dict[str, Any]],
    contracts: list[dict[str, Any]],
    scaling: tuple[int, float, float],
) -> list[dict[str, Any]]:
    """Fill blocks per intent; costs recomputed on each fill's own prices."""
    quantity, slippage, far_mult = scaling
    blocks = []
    for intent in unit["trades"]:
        events: dict[str, dict[str, Any]] = {}
        for event in ("entry", "exit"):
            fill = intent[event]
            costs = side_cost(
                tariff_profile(slippage),
                list(zip(contracts, (fill["near_price"], fill["far_price"]), strict=True)),
                quantity,
            )
            if any(not math.isfinite(value) or value < 0 for value in costs.values()):
                raise W0Error("Некорректные издержки пары.")
            events[event] = fill_block(
                fill["date"],
                fill["spread_fill"],
                fill["near_price"],
                fill["far_price"],
                rows_by_date,
                {key: round(value, 8) for key, value in costs.items()},
                far_value=fill["far_price"] * far_mult,
            )
        blocks.append(events)
    return blocks


def _trade_rows(
    unit: dict[str, Any],
    rows: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    scaling: tuple[float, float, int, float, tuple[list[str], dict[str, float]]],
    funding_known: bool,
    raw_totals: list[tuple[float, float, float]],
) -> list[dict[str, Any]]:
    """Trade rows: gross includes funding, costs come from the recomputed fill blocks."""
    near_mult, far_mult, quantity, margin, benchmark = scaling
    trades = []
    for number, (intent, events) in enumerate(zip(unit["trades"], blocks, strict=True), 1):
        entry, exit_ = intent["entry"], intent["exit"]
        funding = sum(
            float(row["funding_rub"] or 0.0)
            for row in rows
            if entry["date"] <= row["date"] <= exit_["date"]
        )
        direction = 1 if intent["side"] == "long_spread" else -1
        gross = (
            direction
            * quantity
            * (
                (exit_["near_price"] - entry["near_price"]) * near_mult
                - (exit_["far_price"] - entry["far_price"]) * far_mult
            )
            + funding
        )
        cost = sum(sum(events[event]["costs"].values()) for event in ("entry", "exit"))
        if not all(math.isfinite(value) for value in (gross, cost, funding, gross - cost)):
            raise W0Error("Переполнение расчёта сделки.")
        raw_totals.append((round(gross, 8), round(cost, 8), round(gross - cost, 8)))
        trades.append(
            trade_block(
                TradeSpec(
                    number=number,
                    side=intent["side"],
                    quantity=quantity,
                    signal=intent["signal"],
                    entry=events["entry"],
                    exit=events["exit"],
                    exit_reason=intent["exit_reason"],
                    margin_pair=margin,
                    gross_rub=gross,
                    costs_rub=cost,
                    net_rub=gross - cost,
                    open_at_end=intent["open_at_end"],
                    funding_rub=funding if funding_known else None,
                ),
                benchmark_dates=benchmark[0],
                benchmark_prices=benchmark[1],
            )
        )
    return trades


def _curve_step(
    row: dict[str, Any],
    position: dict[str, Any],
    closing: dict[str, Any] | None,
    mults: tuple[float, float],
    quantity: int,
) -> tuple[float, float, dict[str, Any] | None]:
    """Mark change and exit fees of a held position on one row."""
    near_px, far_px = (
        (closing["exit"]["near_price"], closing["exit"]["far_price"])
        if closing
        else (row["near"], row["far"])
    )
    change = (
        position["sign"]
        * quantity
        * ((near_px - position["near"]) * mults[0] - (far_px - position["far"]) * mults[1])
    )
    if closing:
        return change, sum(closing["exit"]["costs"].values()), None
    return change, 0.0, {**position, "near": near_px, "far": far_px}


def _entry_step(
    row: dict[str, Any], opening: dict[str, Any], mults: tuple[float, float], quantity: int
) -> tuple[float, float, dict[str, Any]]:
    """Mark change and entry fees of a position opened on this row."""
    sign = 1 if opening["side"] == "long_spread" else -1
    enter = opening["entry"]
    change = (
        sign
        * quantity
        * (
            (row["near"] - enter["near_price"]) * mults[0]
            - (row["far"] - enter["far_price"]) * mults[1]
        )
    )
    return (
        change,
        sum(enter["costs"].values()),
        {
            "sign": sign,
            "near": row["near"],
            "far": row["far"],
        },
    )


def _excess_drawdown(values: list[float], marks: list[float], capital: float) -> float:
    """Maximum drawdown of the account curve measured against the LQDT benchmark."""
    peak = drawdown = 0.0
    for value, benchmark_mark in zip(values, marks, strict=True):
        excess = round(round(value, 8) - round(capital * benchmark_mark / marks[0], 8), 8)
        peak = max(peak, excess)
        drawdown = max(drawdown, peak - excess)
    return drawdown


def _account_curve(
    template: dict[str, Any],
    unit: dict[str, Any],
    rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    curve: tuple[float, float, int, list[float]],
) -> float:
    """LQDT-aware excess drawdown over the replay of the same trades at sub-kopeck
    precision: the rounded ledger must not be used to calculate risk drawdown.
    """
    near_mult, margin, quantity, marks = curve
    mults = (near_mult, near_mult)
    capital = template["initial_capital_rub"]
    entries = {trade["entry"]["date"]: trade for trade in trades}
    exits = {trade["exit"]["date"]: trade for trade in trades}
    funding_by_date = {row["date"]: float(row["funding_rub"] or 0.0) for row in unit["series"]}
    position: dict[str, Any] | None = None
    cumulative = 0.0
    precise_pnl = []
    for row in rows:
        day = row["date"]
        change = fee = 0.0
        if row["volume_eligible"] and position is not None:
            change, fee, position = _curve_step(row, position, exits.get(day), mults, quantity)
        opening = entries.get(day) if row["volume_eligible"] else None
        if opening:
            moved, cost, position = _entry_step(row, opening, mults, quantity)
            change += moved
            fee += cost
        cumulative += change - fee + funding_by_date[day] * quantity
        precise_pnl.append(round(capital + cumulative, 8) - capital)
    values, _ = lqdt_overlay_values(
        capital, precise_pnl, [margin if row["position"] else 0.0 for row in ledger], marks
    )
    return _excess_drawdown(values, marks, capital)


def _summary_update(
    template: dict[str, Any],
    trades: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    drawdown: float,
    raw_totals: list[tuple[float, float, float]],
) -> None:
    closed = [trade for trade in trades if not trade["open_at_end"]]
    gross, costs, net = (
        sum(trade[key] for trade in trades) for key in ("gross_rub", "costs_rub", "net_rub")
    )
    hurdle = sum(trade["lqdt_on_margin_rub"] for trade in trades)
    template["summary"].update(
        trades=len(trades),
        closed_trades=len(closed),
        open_positions=len(trades) - len(closed),
        winners=sum(trade["net_rub"] > 0 for trade in closed),
        gross_rub=round(gross, 2),
        costs_rub=round(costs, 2),
        net_rub=round(net, 2),
        cost_totals=_cost_totals(trades),
        lqdt_on_margin_rub=round(hurdle, 2),
        excess_over_lqdt_rub=round(net - hurdle, 2),
        max_excess_drawdown_rub=round(drawdown, 2),
        ledger_reconciliation_rub=round(
            net - (ledger[-1]["cumulative_net_rub"] if ledger else 0), 2
        ),
    )
    totals = [math.fsum(row[i] for row in raw_totals) for i in range(3)]
    apply_raw_trade_totals(template["summary"], trades, ledger, totals)
    if abs(template["summary"]["ledger_reconciliation_rub"]) > 0.02:
        raise W0Error("Учёт плагина не согласован с журналом сделок.")


def fixed_daily_pair_plugin_payload(
    template: dict[str, Any],
    unit: dict[str, Any],
    context: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    execution: dict[str, Any],
    benchmark: tuple[list[str], dict[str, float]],
) -> dict[str, Any]:
    """Recompute fixed-quantity fills, cash flows and journal from unit intents.

    `context` carries `(series, pair, provenance)` from the native template proof.
    """
    series, pair, provenance = context
    quantity, slippage = execution["quantity"], execution["slippage_ticks"]
    near, far = pair["near"], pair["far"]
    contracts = [series["contracts"][near], series["contracts"][far]]
    multipliers = (point_value(contracts[0]), point_value(contracts[1]))
    near_mult, far_mult = multipliers
    margin = sum(float(contract["initial_margin"]) for contract in contracts) * quantity
    rows = _scaled_series(unit, template, quantity)
    blocks = _trade_blocks(
        unit, {r["date"]: r for r in rows}, contracts, (quantity, slippage, far_mult)
    )
    funding_known = bool(series["provenance"].get("near_funding_history_sha256") or provenance)
    raw_totals: list[tuple[float, float, float]] = []
    trades = _trade_rows(
        unit,
        rows,
        blocks,
        (near_mult, far_mult, quantity, margin, benchmark),
        funding_known,
        raw_totals,
    )
    ledger = price_ledger(rows, trades, near_mult, far_mult, quantity)
    for row, entry in zip(rows, ledger, strict=True):
        row["margin_rub"] = round(margin, 2) if entry["position"] else 0.0
    marks = [
        mark(benchmark[0], benchmark[1], row.get("benchmark_mark_date") or row["date"])
        for row in rows
    ]
    drawdown = _account_curve(
        template, unit, rows, trades, ledger, (near_mult, margin, quantity, marks)
    )
    template["selection"].update(
        strategy=unit["selection"]["strategy"], parameters=unit["selection"]["parameters"]
    )
    template.update(series=rows, trades=trades, ledger=ledger)
    _summary_update(template, trades, ledger, drawdown, raw_totals)
    template["provenance"].update({**provenance, **unit["provenance"]})
    template["notes"] = [
        note for note in template["notes"] if not note.startswith(OPEN_POSITION_PREFIX)
    ]
    if any(trade["open_at_end"] for trade in trades):
        template["notes"].append(OPEN_POSITION_NOTE)
    template["notes"].append(PLUGIN_SOURCE_NOTE)
    attach_entry_levels(template)
    template["run_id"] = _run_id(
        {
            "selection": template["selection"],
            "series": rows,
            "trades": trades,
            "ledger": ledger,
            "provenance": template["provenance"],
        }
    )
    return template
