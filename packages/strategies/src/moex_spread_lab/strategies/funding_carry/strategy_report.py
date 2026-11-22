"""Report assembly for frozen carry hypothesis evaluation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.portfolio import lqdt_overlay_values

from .study import CARRY_SCHEMA, STRATEGY_SCHEMA, CarryStrategyError


@dataclass
class CarryConfig:
    transaction_cost_scenario: str = "base"
    initial_capital: float = 30000.0
    warmup_observations: int = 20
    expiry_buffer_days: int = 5
    assignment_window_trading_days: int = 10
    strategy_variant: str = "basis_median"
    quantity: int = 1
    execution_delay_rows: int = 1
    cash_in_lqdt: bool = False
    slippage_ticks: float = 1.0
    metrics_only: bool = False


def parse_carry_config(options: dict[str, Any]) -> CarryConfig:
    allowed = set(CarryConfig.__dataclass_fields__)
    unknown = set(options) - allowed
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {sorted(unknown)}")
    cfg = CarryConfig(
        transaction_cost_scenario=str(options.get("transaction_cost_scenario", "base")),
        initial_capital=float(options.get("initial_capital", 30000.0)),
        warmup_observations=int(options.get("warmup_observations", 20)),
        expiry_buffer_days=int(options.get("expiry_buffer_days", 5)),
        assignment_window_trading_days=int(options.get("assignment_window_trading_days", 10)),
        strategy_variant=str(options.get("strategy_variant", "basis_median")),
        quantity=int(options.get("quantity", 1)),
        execution_delay_rows=int(options.get("execution_delay_rows", 1)),
        cash_in_lqdt=bool(options.get("cash_in_lqdt", False)),
        slippage_ticks=float(options.get("slippage_ticks", 1.0)),
        metrics_only=bool(options.get("metrics_only", False)),
    )
    if (
        cfg.initial_capital <= 0
        or cfg.warmup_observations < 1
        or cfg.expiry_buffer_days < 0
        or cfg.assignment_window_trading_days < 1
        or cfg.strategy_variant not in {"basis_median", "funding_only"}
        or cfg.quantity < 1
        or cfg.execution_delay_rows not in (0, 1)
        or (cfg.execution_delay_rows == 0 and cfg.strategy_variant != "funding_only")
    ):
        raise CarryStrategyError("invalid carry strategy parameters")
    return cfg


def resolve_expiry_windows(
    rows: list[dict[str, Any]],
    far: str,
    far_expiry: Any,
    expiry_buffer_days: int,
    assignment_days: int,
) -> tuple[str, str | None, str]:
    if not isinstance(far_expiry, str):
        raise CarryStrategyError(f"invalid expiry for {far}")
    try:
        expiry_date = date.fromisoformat(far_expiry)
    except ValueError as exc:
        raise CarryStrategyError(f"invalid expiry for {far}") from exc
    expiry_cutoff_iso = (expiry_date - timedelta(days=expiry_buffer_days)).isoformat()
    observed = [
        date.fromisoformat(row["date"])
        for row in rows
        if date.fromisoformat(row["date"]) < expiry_date
    ]
    if (
        observed
        and observed[-1] >= expiry_date - timedelta(days=15)
        and len(observed) >= assignment_days
    ):
        return (
            expiry_cutoff_iso,
            observed[-assignment_days].isoformat(),
            "observed_common_eod_calendar",
        )
    return expiry_cutoff_iso, None, "not_observable_in_input_window"


def append_carry_daily_row(
    ctx: Any,
    state: Any,
    row: dict[str, Any],
    index: int,
    date_key: str,
    pnls: tuple[float, float],
    bench_pair: tuple[Callable[[str], dict[str, Any]] | None, dict[str, Any] | None],
) -> None:
    increment, funding_included = pnls
    if ctx.cfg.metrics_only:
        state.daily.append(
            {
                "date": date_key,
                "position": 1 if state.position else 0,
                "gross_pnl_increment": round(increment, 8),
                "near_settle": row.get("near_settle"),
                "far_settle": row.get("far_settle"),
                "daily_funding_included_rub": round(funding_included, 8),
            }
        )
        return
    benchmark_mark, start_benchmark = bench_pair
    assert benchmark_mark is not None and start_benchmark is not None
    b_row = benchmark_mark(date_key)
    strat_val = ctx.cfg.initial_capital + state.cumulative_gross - state.cumulative_cost
    bench_val = ctx.cfg.initial_capital * float(b_row["price"]) / float(start_benchmark["price"])
    state.daily.append(
        {
            "date": date_key,
            "position": 1 if state.position else 0,
            "gross_pnl_increment": round(increment, 8),
            "cumulative_gross_pnl": round(state.cumulative_gross, 8),
            "cumulative_transaction_costs": round(state.cumulative_cost, 8),
            "strategy_value_proxy": round(strat_val, 8),
            "benchmark_value": round(bench_val, 8),
            "X": round(strat_val - bench_val, 8),
            "benchmark_source_kind": b_row["source_kind"],
            "benchmark_source_ticker": b_row["source_ticker"],
            "benchmark_mark_kind": b_row["mark_kind"],
            "valuation_status": "EOD_settlement_proxy_not_executable_fill",
            "near_settle": row.get("near_settle"),
            "far_settle": row.get("far_settle"),
            "near_volume": row.get("near_volume"),
            "far_volume": row.get("far_volume"),
            "daily_basis_pnl_contract": float(
                row.get("daily_basis_pnl_short_near_long_far") or 0.0
            ),
            "daily_funding_contract": ctx.funding_vals[index],
            "daily_funding_included_rub": round(funding_included, 8),
        }
    )


def assemble_carry_report(
    ctx: Any,
    state: Any,
    meta: dict[str, Any],
) -> dict[str, Any]:
    cfg = ctx.cfg
    near: str = meta["near"]
    far: str = meta["far"]
    far_expiry: str = meta["far_expiry"]
    assignment_status: str = meta["assignment_status"]
    contracts: dict[str, dict[str, Any]] = meta["contracts"]
    contracts_hash: str = meta["contracts_hash"]
    contracts_path: Path = meta["contracts_path"]
    benchmark_path: Path = meta["benchmark_path"]
    benchmark_hash: str = meta["benchmark_hash"]
    benchmark_mark: Callable[[str], dict[str, Any]] = meta["benchmark_mark"]
    cost_profile: dict[str, Any] = meta["cost_profile"]
    account_risk: dict[str, Any] = meta["account_risk"]

    shortfalls = 0
    if cfg.cash_in_lqdt:
        margin = (
            float(contracts[near]["initial_margin"]) + float(contracts[far]["initial_margin"])
        ) * cfg.quantity
        values, shortfalls = lqdt_overlay_values(
            cfg.initial_capital,
            [
                row["cumulative_gross_pnl"] - row["cumulative_transaction_costs"]
                for row in state.daily
            ],
            [margin if row["position"] else 0.0 for row in state.daily],
            [float(benchmark_mark(row["date"])["price"]) for row in state.daily],
        )
        for row, value in zip(state.daily, values, strict=True):
            row["strategy_value_proxy"] = round(value, 8)
            row["X"] = round(value - row["benchmark_value"], 8)
    peak = 0.0
    for row in state.daily:
        peak = max(peak, float(row["X"]))
        row["H"] = round(peak, 8)
        row["D"] = round(peak - float(row["X"]), 8)
    closed = [t for t in state.trades if t["status"] == "closed_proxy_round_trip"]
    is_basis = cfg.strategy_variant == "basis_median"
    w_obs = cfg.warmup_observations
    assignment_event = {
        "event_type": "perpetual_to_quarterly_execution_window",
        "far_contract": far,
        "far_expiry": far_expiry,
        "window_length_trading_days": cfg.assignment_window_trading_days,
        "window_start_date": ctx.assignment_start_iso,
        "status": assignment_status,
        "policy": "exit_before_perpetual_execution_window",
        "payment_rate": 0.03,
        "payment_modelled": False,
        "payment_note": "No 3% execution payment is added unless the window is observable in the input.",
        "source": "MOEX notice n89495",
    }
    return {
        "schema_version": STRATEGY_SCHEMA,
        "release_state": "exploratory_inconclusive",
        "study_role": "frozen_funding_filtered_basis_strategy_proxy_backtest",
        "near": near,
        "far": far,
        "direction": "short_near_long_far",
        "rule_id": "H2-S1" if is_basis else "H3-F1",
        "hypothesis": "funding_filtered_basis_reversion" if is_basis else "funding_only_carry",
        "parameters": {
            "warmup_observations": w_obs,
            "trailing_funding_condition": f"mean_previous_{w_obs}_daily_funding_received > 0",
            "entry_condition": (
                f"current_basis <= median_previous_{w_obs}_basis"
                if is_basis
                else f"mean_previous_{w_obs}_daily_funding_received > 0"
            ),
            "exit_conditions": [
                *([f"current_basis >= median_previous_{w_obs}_basis"] if is_basis else []),
                "mean_previous_20_daily_funding_received <= 0",
                "date >= far_expiry - 5 calendar days",
            ],
            "decision_delay_observations": 1,
            "execution_delay_rows": cfg.execution_delay_rows,
            "accounting": "free_cash_in_lqdt_margin_cash_idle"
            if cfg.cash_in_lqdt
            else "idle_cash_no_yield",
            "quantity_per_leg": cfg.quantity,
            "expiry": far_expiry,
            "expiry_buffer_days": cfg.expiry_buffer_days,
            "assignment_window_trading_days": cfg.assignment_window_trading_days,
            "assignment_policy": "exit_before_perpetual_execution_window",
            "assignment_window_start_date": ctx.assignment_start_iso,
            "assignment_window_status": assignment_status,
            "assignment_event": assignment_event,
        },
        "cost_scenario": cost_profile,
        "account_risk": account_risk,
        "summary": {
            "first_date": state.daily[0]["date"],
            "last_date": state.daily[-1]["date"],
            "row_count": len(state.daily),
            "signal_count": len(state.signals),
            "closed_trade_count": len(closed),
            "open_trade_count": sum(
                t["status"] == "open_at_data_end_no_executable_exit" for t in state.trades
            ),
            "cash_shortfall_rows": shortfalls,
            "final_strategy_value_proxy": state.daily[-1]["strategy_value_proxy"],
            "final_benchmark_value": state.daily[-1]["benchmark_value"],
            "final_X": state.daily[-1]["X"],
            "final_H": state.daily[-1]["H"],
            "final_D": state.daily[-1]["D"],
            "gross_pnl": round(state.cumulative_gross, 8),
            "transaction_costs": round(state.cumulative_cost, 8),
            "historical_executable_fills": False,
            "lqdt_benchmark_observed_or_proxy": sorted(
                {row["benchmark_source_kind"] for row in state.daily}
            ),
            "perpetual_assignment_event_observed": any(
                s["reason"] == "perpetual_execution_window" for s in state.signals
            ),
            "account_risk_gate_current_snapshot": account_risk["summary"][
                "current_snapshot_passed"
            ],
            "account_risk_gate_all_working_scenarios": account_risk["summary"][
                "all_scenarios_passed"
            ],
        },
        "trades": state.trades,
        "signals": state.signals,
        "daily": state.daily,
        "provenance": {
            "carry_decomposition_schema": CARRY_SCHEMA,
            "benchmark_sha256": benchmark_hash,
            "benchmark_path": str(benchmark_path),
            "contracts_sha256": contracts_hash,
            "contracts_path": str(contracts_path),
            "historical_specification_status": "current_snapshot_fallback_assumed",
            "execution_evidence_status": "historical_bid_ask_unavailable",
        },
    }
