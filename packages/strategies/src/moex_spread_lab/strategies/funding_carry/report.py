"""Carry-strategy transaction cost profiles and report payload assembly."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.costs import (
    LqdtOverlay,
    lqdt_overlay_values,
    side_cost,
    tariff_profile,
)

from .study import CARRY_SCHEMA, STRATEGY_SCHEMA, CarryStrategyError, _round

_ILLUSTRATIVE_LEVELS: dict[str, float] = {"low": 0.5, "base": 1.0, "stress": 2.0}


def transaction_cost_profile(
    name: str,
    tick_value: float,
    *,
    near_price: float | None = None,
    far_price: float | None = None,
    near_lot_size: int = 1000,
    far_lot_size: int = 1000,
    far_tick_value: float | None = None,
) -> dict[str, Any]:
    if name in {"moex_current_taker", "vtb_online_prime_current"}:
        if near_price is None or far_price is None:
            raise CarryStrategyError("MOEX tariff scenario requires reference prices")
        rate = 0.0000462
        near_side, far_side = near_price * near_lot_size * rate, far_price * far_lot_size * rate
        side_total = _round(near_side + far_side)
        return {
            "scenario": name,
            "exchange_clearing_rate": rate,
            "vtb_broker_rate": 0.0,
            "vtb_broker_commission": 0.0,
            "vtb_tariff_effective_to": "2026-12-31",
            "near_reference_price": _round(near_price),
            "far_reference_price": _round(far_price),
            "near_leg_side_cost": _round(near_side),
            "far_leg_side_cost": _round(far_side),
            "entry_cost": side_total,
            "exit_cost": side_total,
            "round_trip_cost": _round(2 * (near_side + far_side)),
            "status": (
                "current_moex_rate_plus_user_confirmed_vtb_zero_derivatives_broker_fee"
                if name == "vtb_online_prime_current"
                else "current_official_moex_taker_rate_account_broker_unverified"
            ),
            "source": ["https://www.moex.com/s93", "docs/inputs/moy_online_praym.pdf"],
            "funding_component": "actual_SWAPRATE_included;_broker_and_other_costs_not_included",
        }
    if (level := _ILLUSTRATIVE_LEVELS.get(name)) is None:
        raise CarryStrategyError(f"unknown transaction cost scenario: {name}")
    slip = level * (tick_value + (tick_value if far_tick_value is None else far_tick_value))
    entry_cost, exit_cost = _round(6.0 * level + slip), _round(8.0 * level + slip)
    return {
        "exchange": level,
        "clearing": level,
        "broker": level,
        "slippage_ticks": level,
        "exit": level,
        "entry_cost": entry_cost,
        "exit_cost": exit_cost,
        "round_trip_cost": _round(entry_cost + exit_cost),
        "scenario": name,
        "funding_component": "actual_SWAPRATE_included;_illustrative_funding_cost_not_added",
        "status": "illustrative_unverified",
    }


def build_cost_profile(
    scenario: str,
    slip_ticks: float,
    near_c: dict[str, Any],
    far_c: dict[str, Any],
    last: dict[str, Any],
) -> dict[str, Any]:
    n_ref = float(last["near_settle"]) if "near_settle" in last else None
    f_ref = float(last["far_settle"]) if "far_settle" in last else None
    if scenario == "tariff":
        if n_ref is None or f_ref is None:
            raise CarryStrategyError("tariff scenario requires settlement prices")
        tariff = tariff_profile(slip_ticks)
        side = sum(
            side_cost(
                tariff, [(near_c, n_ref), (far_c, f_ref)], 1, as_of=str(last["date"])
            ).values()
        )
        return {
            **tariff,
            "entry_cost": _round(side),
            "exit_cost": _round(side),
            "round_trip_cost": _round(2 * side),
            "near_reference_price": n_ref,
            "far_reference_price": f_ref,
        }
    return transaction_cost_profile(
        scenario,
        float(near_c["tick_value"]),
        near_price=n_ref,
        far_price=f_ref,
        near_lot_size=int(near_c["lot_size"]),
        far_lot_size=int(far_c["lot_size"]),
        far_tick_value=float(far_c["tick_value"]),
    )


def _finalize_daily(
    s: Any, margin_leg_sum: float, mark_fn: Callable[[str], dict[str, Any]]
) -> LqdtOverlay | None:
    overlay = None
    if s.cfg.cash_in_lqdt:
        margin = margin_leg_sum * s.cfg.qty
        overlay = lqdt_overlay_values(
            s.cfg.capital,
            [r["cumulative_gross_pnl"] - r["cumulative_transaction_costs"] for r in s.daily],
            [margin if r["position"] else 0.0 for r in s.daily],
            [float(mark_fn(r["date"])["price"]) for r in s.daily],
        )
        for r, val in zip(s.daily, overlay.values, strict=True):
            r["strategy_value_proxy"], r["X"] = _round(val), _round(val - r["benchmark_value"])
    peak = 0.0
    for r in s.daily:
        peak = max(peak, float(r["X"]))
        r["H"], r["D"] = _round(peak), _round(peak - float(r["X"]))
    return overlay


def build_carry_report(
    s: Any,
    leg_info: tuple[str, str, str, str, dict[str, dict[str, Any]], str, Path],
    run_info: tuple[Path, str, Callable[[str], dict[str, Any]], dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    near, far, far_exp, assign_status, contracts, contracts_hash, contracts_path = leg_info
    bench_path, bench_hash, mark_fn, cost_prof, risk = run_info
    cfg, w_obs = s.cfg, s.cfg.warmup
    margin_sum = float(contracts[near]["initial_margin"]) + float(contracts[far]["initial_margin"])
    overlay = _finalize_daily(s, margin_sum, mark_fn)
    is_basis, last = cfg.variant == "basis_median", s.daily[-1]
    pos_f = f"mean_previous_{w_obs}_daily_funding_received > 0"
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
            "trailing_funding_condition": pos_f,
            "entry_condition": f"current_basis <= median_previous_{w_obs}_basis"
            if is_basis
            else pos_f,
            "exit_conditions": [
                *([f"current_basis >= median_previous_{w_obs}_basis"] if is_basis else []),
                f"mean_previous_{w_obs}_daily_funding_received <= 0",
                "date >= far_expiry - 5 calendar days",
            ],
            "decision_delay_observations": 1,
            "execution_delay_rows": cfg.delay,
            "accounting": "free_cash_in_lqdt_margin_cash_idle"
            if cfg.cash_in_lqdt
            else "idle_cash_no_yield",
            "quantity_per_leg": cfg.qty,
            "expiry": far_exp,
            "expiry_buffer_days": cfg.buf_days,
            "assignment_window_trading_days": cfg.assign_days,
            "assignment_policy": "exit_before_perpetual_execution_window",
            "assignment_window_start_date": s.assign_start,
            "assignment_window_status": assign_status,
            "assignment_event": {
                "event_type": "perpetual_to_quarterly_execution_window",
                "far_contract": far,
                "far_expiry": far_exp,
                "window_length_trading_days": cfg.assign_days,
                "window_start_date": s.assign_start,
                "status": assign_status,
                "policy": "exit_before_perpetual_execution_window",
                "payment_rate": 0.03,
                "payment_modelled": False,
                "payment_note": "No 3% execution payment is added unless the window is observable in the input.",
                "source": "MOEX notice n89495",
            },
        },
        "cost_scenario": cost_prof,
        "account_risk": risk,
        "summary": {
            "first_date": s.daily[0]["date"],
            "last_date": last["date"],
            "row_count": len(s.daily),
            "signal_count": len(s.signals),
            "closed_trade_count": sum(t["status"] == "closed_proxy_round_trip" for t in s.trades),
            "open_trade_count": sum(
                t["status"] == "open_at_data_end_no_executable_exit" for t in s.trades
            ),
            "cash_shortfall_rows": overlay.shortfall_rows if overlay else 0,
            "final_strategy_value_proxy": last["strategy_value_proxy"],
            "final_benchmark_value": last["benchmark_value"],
            "final_X": last["X"],
            "final_H": last["H"],
            "final_D": last["D"],
            "gross_pnl": _round(s.cum_gross),
            "transaction_costs": _round(s.cum_cost),
            "historical_executable_fills": False,
            "lqdt_benchmark_observed_or_proxy": sorted(
                {r["benchmark_source_kind"] for r in s.daily}
            ),
            "perpetual_assignment_event_observed": any(
                sig["reason"] == "perpetual_execution_window" for sig in s.signals
            ),
            "account_risk_gate_current_snapshot": risk["summary"]["current_snapshot_passed"],
            "account_risk_gate_all_working_scenarios": risk["summary"]["all_scenarios_passed"],
        },
        "trades": s.trades,
        "signals": s.signals,
        "daily": s.daily,
        "provenance": {
            "carry_decomposition_schema": CARRY_SCHEMA,
            "benchmark_sha256": bench_hash,
            "benchmark_path": str(bench_path),
            "contracts_sha256": contracts_hash,
            "contracts_path": str(contracts_path),
            "historical_specification_status": "current_snapshot_fallback_assumed",
            "execution_evidence_status": "historical_bid_ask_unavailable",
        },
    }
