"""Report assembly for ordinary pair mean reversion baseline."""

from __future__ import annotations

from typing import Any

from moex_spread_lab.accounting.portfolio import lqdt_overlay_values

from .series_builder import ANALYTICS_SCHEMA, _round


def assemble_baseline_report(
    series: dict[str, Any],
    rows: list[dict[str, Any]],
    ctx: Any,
    state: Any,
) -> dict[str, Any]:
    params = ctx.params
    cash_shortfall_rows = 0
    if params.cash_in_lqdt:
        values, cash_shortfall_rows = lqdt_overlay_values(
            params.initial_capital,
            [row["strategy_value"] - params.initial_capital for row in state.daily],
            [
                ctx.margin_per_pair * params.quantity if row["position"] else 0.0
                for row in state.daily
            ],
            [float(row["benchmark_price"]) for row in rows],
        )
        for row, value in zip(state.daily, values, strict=True):
            row["strategy_value"] = _round(value)
            row["X"] = _round(value - row["benchmark_value"])
    peak = 0.0
    for row in state.daily:
        peak = max(peak, float(row["X"]))
        row["H"] = _round(peak)
        row["D"] = _round(peak - float(row["X"]))
    final = state.daily[-1]
    net_status = (
        "usable_for_gross_only"
        if ctx.normalized_cost is None
        else "published_tariff_net"
        if ctx.tariff_costs
        else "illustrative_net_sensitivity"
    )
    return {
        "schema_version": ANALYTICS_SCHEMA,
        "release_state": "unit_signal_intents_not_portfolio"
        if params.sizing_intents
        else "exploratory_inconclusive",
        "near": series["near"],
        "far": series["far"],
        "direction": series.get("direction", "near_minus_far"),
        "analysis_role": params.analysis_role,
        "timeframe": series.get("timeframe", "1d"),
        "price_interpretation": "ISS candle close analytical proxy under A-06",
        "execution_evidence_status": "historical_bid_ask_unavailable",
        "parameters": {
            "initial_capital": params.initial_capital,
            "lookback": params.lookback,
            "entry_z": params.entry_z,
            "exit_z": params.exit_z,
            "quantity": params.quantity,
            "decision_delay_bars": params.decision_delay_bars,
            "fill": params.fill,
            "accounting": "free_cash_in_lqdt_margin_cash_idle"
            if params.cash_in_lqdt
            else "idle_cash_no_yield",
            "margin_per_pair": _round(ctx.margin_per_pair),
            "margin_gate": "deferred_to_funded_sizing" if params.sizing_intents else "passed",
        },
        "economics": {
            "price_unit": series.get("price_unit", "RUB_per_CNY"),
            "near_lot_size": ctx.near_contract["lot_size"],
            "far_lot_size": ctx.far_contract["lot_size"],
            "near_tick_size": ctx.near_contract["tick_size"],
            "far_tick_size": ctx.far_contract["tick_size"],
            "near_tick_value_rub": ctx.near_contract["tick_value"],
            "far_tick_value_rub": ctx.far_contract["tick_value"],
            "pair_initial_margin_rub": _round(ctx.margin_per_pair * params.quantity),
            "one_spread_tick_value_rub": _round(
                float(series.get("spread_tick_value_rub", ctx.near_contract["tick_value"]))
            ),
            "contract_terms_status": "current_snapshot_fallback_assumed",
        },
        "costs": {
            "status": ctx.cost_status,
            "components": list(ctx.cost_components),
            "profile": ctx.normalized_cost,
            "total_by_component": {k: _round(v) for k, v in state.cost_totals.items()}
            if ctx.normalized_cost is not None
            else None,
            "net_status": net_status,
        },
        "summary": {
            "common_row_count": len(rows),
            "first_date": rows[0]["date"],
            "last_date": rows[-1]["date"],
            "trade_count": len(state.trades),
            "gross_pnl": _round(state.realized_gross + state.cumulative_funding),
            "price_gross_pnl": _round(state.realized_gross),
            "funding_rub": _round(state.cumulative_funding),
            "modeled_costs": _round(state.realized_costs)
            if ctx.normalized_cost is not None
            else None,
            "costs_applied": ctx.normalized_cost is not None,
            "final_strategy_value": final["strategy_value"],
            "final_benchmark_value": final["benchmark_value"],
            "final_X": final["X"],
            "max_drawdown_D": _round(max(float(row["D"]) for row in state.daily)),
            "min_X": _round(min(float(row["X"]) for row in state.daily)),
            "benchmark_source_kinds": sorted({row["benchmark_source_kind"] for row in state.daily}),
            "benchmark_mark_kinds": sorted(
                {row.get("benchmark_mark_kind", "daily_close_same_date") for row in state.daily}
            ),
            "cash_shortfall_rows": cash_shortfall_rows,
        },
        "trades": state.trades,
        "daily": state.daily,
        "input_provenance": series["provenance"],
    }


def record_daily_step(
    ctx: Any,
    state: Any,
    row: dict[str, Any],
    action: str,
    signal_z: float | None,
    funding_today: float,
    vals: tuple[float, float],
) -> None:
    if ctx.fast_intents:
        if ctx.params.funding_swaprates:
            state.daily.append(
                {
                    "date": row["date"],
                    "action": action,
                    "position": state.position,
                    "funding_rub": _round(funding_today),
                }
            )
        return
    benchmark_start, unrealized_pnl = vals
    bench_val = ctx.params.initial_capital * float(row["benchmark_price"]) / benchmark_start
    strat_val = (
        ctx.params.initial_capital
        + state.realized_gross
        - state.realized_costs
        + state.cumulative_funding
        + unrealized_pnl
    )
    state.daily.append(
        {
            "date": row["date"],
            "timestamp": row.get("timestamp", row["date"]),
            "spread_ticks": row["spread_ticks"],
            "signal_z": _round(signal_z) if signal_z is not None else None,
            "action": action,
            "position": state.position,
            "strategy_value": _round(strat_val),
            "benchmark_value": _round(bench_val),
            "X": _round(strat_val - bench_val),
            "gross_realized_pnl": _round(state.realized_gross + state.cumulative_funding),
            "funding_rub": _round(funding_today),
            "cumulative_funding_rub": _round(state.cumulative_funding),
            "modeled_costs": _round(state.realized_costs)
            if ctx.normalized_cost is not None
            else None,
            "benchmark_source_kind": row["benchmark_source_kind"],
            "benchmark_mark_kind": row.get("benchmark_mark_kind", "daily_close_same_date"),
        }
    )
