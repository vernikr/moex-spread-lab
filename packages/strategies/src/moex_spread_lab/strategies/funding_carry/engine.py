"""Frozen H2/H3 carry hypothesis simulation, LQDT benchmark alignment, and report assembly."""

from __future__ import annotations

import statistics
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.accounting.account_risk import assess_vtb_pair
from moex_spread_lab.accounting.costs import lqdt_overlay_values, side_cost, tariff_profile
from moex_spread_lab.core.hashed_json_cache import HashedJSONCache
from moex_spread_lab.core.w0 import W0Error, load_contracts, read_hashed_json

from .study import CARRY_SCHEMA, STRATEGY_SCHEMA, CarryStrategyError, _round

__all__ = ["build_carry_strategy", "transaction_cost_profile"]

_BENCHMARK_CACHE: HashedJSONCache[tuple[str, int, int], tuple[dict[str, dict[str, Any]], str]] = (
    HashedJSONCache(64)
)
_ILLUSTRATIVE_LEVELS: dict[str, float] = {"low": 0.5, "base": 1.0, "stress": 2.0}


def _benchmark_rows(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    resolved = path.resolve()
    try:
        stat = resolved.stat()
    except OSError as exc:
        raise CarryStrategyError(f"cannot read LQDT benchmark {path}: {exc}") from exc
    key = (str(resolved), stat.st_mtime_ns, stat.st_size)
    if (cached := _BENCHMARK_CACHE.get(key)) is not None:
        return cached
    try:
        raw, digest = read_hashed_json(resolved, error=CarryStrategyError, label="LQDT benchmark")
    except CarryStrategyError:
        raise
    except W0Error as exc:
        raise CarryStrategyError(str(exc)) from exc
    payload = cast(Any, raw)
    if (
        not isinstance(payload, dict)
        or cast(dict[str, Any], payload).get("schema_version") != "lqdt-lineage.v1"
    ):
        raise CarryStrategyError("unsupported LQDT benchmark schema")
    rows: dict[str, dict[str, Any]] = {}
    for item in cast(dict[str, Any], payload).get("rows", []):
        if not isinstance(item, dict) or not isinstance(
            cast(dict[str, Any], item).get("date"), str
        ):
            raise CarryStrategyError("malformed LQDT benchmark row")
        row = cast(dict[str, Any], item)
        try:
            price = float(row["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CarryStrategyError(f"invalid LQDT benchmark price on {row.get('date')}") from exc
        if price <= 0:
            raise CarryStrategyError(f"LQDT benchmark price must be positive on {row['date']}")
        rows[row["date"]] = {
            "date": row["date"],
            "price": price,
            "source_kind": row.get("source_kind", "unknown"),
            "source_ticker": row.get("source_ticker", "unknown"),
        }
    if not rows:
        raise CarryStrategyError("empty LQDT benchmark")
    _BENCHMARK_CACHE.put(key, (rows, digest))
    return rows, digest


def _benchmark_lookup(
    path: Path, first_date: str
) -> tuple[Callable[[str], dict[str, Any]], dict[str, Any], str]:
    benchmark, digest = _benchmark_rows(path)
    dates = sorted(benchmark)

    def mark(d: str) -> dict[str, Any]:
        idx = bisect_left(dates, d)
        if idx < len(dates) and dates[idx] == d:
            return {**benchmark[d], "mark_kind": "daily_close_same_date"}
        if idx == 0:
            raise CarryStrategyError(f"benchmark has no mark on or before {d}")
        return {**benchmark[dates[idx - 1]], "mark_kind": "prior_daily_close"}

    return mark, mark(first_date), digest


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


def _cost_profile(
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
        side = sum(side_cost(tariff, [(near_c, n_ref), (far_c, f_ref)], 1).values())
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


@dataclass
class _Cfg:
    cost_scenario: str
    capital: float
    warmup: int
    buf_days: int
    assign_days: int
    variant: str
    qty: int
    delay: int
    cash_in_lqdt: bool
    slip_ticks: float
    metrics_only: bool


_CFG_KEYS = frozenset(
    {
        "transaction_cost_scenario",
        "initial_capital",
        "warmup_observations",
        "expiry_buffer_days",
        "assignment_window_trading_days",
        "strategy_variant",
        "quantity",
        "execution_delay_rows",
        "cash_in_lqdt",
        "slippage_ticks",
        "metrics_only",
    }
)


def _parse_cfg(opts: dict[str, Any]) -> _Cfg:
    if unknown := set(opts) - _CFG_KEYS:
        raise TypeError(f"unexpected keyword argument(s): {sorted(unknown)}")
    cfg = _Cfg(
        str(opts.get("transaction_cost_scenario", "base")),
        float(opts.get("initial_capital", 30000.0)),
        int(opts.get("warmup_observations", 20)),
        int(opts.get("expiry_buffer_days", 5)),
        int(opts.get("assignment_window_trading_days", 10)),
        str(opts.get("strategy_variant", "basis_median")),
        int(opts.get("quantity", 1)),
        int(opts.get("execution_delay_rows", 1)),
        bool(opts.get("cash_in_lqdt", False)),
        float(opts.get("slippage_ticks", 1.0)),
        bool(opts.get("metrics_only", False)),
    )
    if (
        cfg.capital <= 0
        or cfg.warmup < 1
        or cfg.buf_days < 0
        or cfg.assign_days < 1
        or cfg.variant not in {"basis_median", "funding_only"}
        or cfg.qty < 1
        or cfg.delay not in (0, 1)
        or (cfg.delay == 0 and cfg.variant != "funding_only")
    ):
        raise CarryStrategyError("invalid carry strategy parameters")
    return cfg


def _expiry_windows(
    rows: list[dict[str, Any]], far: str, far_exp: Any, buf_days: int, assign_days: int
) -> tuple[str, str | None, str]:
    if not isinstance(far_exp, str):
        raise CarryStrategyError(f"invalid expiry for {far}")
    try:
        exp = date.fromisoformat(far_exp)
    except ValueError as exc:
        raise CarryStrategyError(f"invalid expiry for {far}") from exc
    cutoff = (exp - timedelta(days=buf_days)).isoformat()
    obs = [date.fromisoformat(r["date"]) for r in rows if date.fromisoformat(r["date"]) < exp]
    if obs and obs[-1] >= exp - timedelta(days=15) and len(obs) >= assign_days:
        return cutoff, obs[-assign_days].isoformat(), "observed_common_eod_calendar"
    return cutoff, None, "not_observable_in_input_window"


@dataclass
class _Sim:
    cfg: _Cfg
    rows: list[dict[str, Any]]
    entry_cost: float
    exit_cost: float
    cutoff: str
    assign_start: str | None
    funding: list[float]
    known_on: list[str]
    basis: list[float] | None
    pos: bool = False
    entry_idx: int | None = None
    entry_dec: str | None = None
    pending: dict[str, Any] | None = None
    cum_gross: float = 0.0
    cum_cost: float = 0.0
    trade_gross: float = 0.0
    trades: list[dict[str, Any]] = field(default_factory=list)
    daily: list[dict[str, Any]] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)


def _exec_order(s: _Sim, order: dict[str, Any], idx: int, d: str) -> None:
    if order["action"] == "entry":
        if s.pos:
            raise CarryStrategyError("entry attempted while a position is open")
        s.pos, s.entry_idx, s.entry_dec, s.trade_gross = True, idx, order["decision_date"], 0.0
        s.cum_cost += s.entry_cost
        return
    if not s.pos or s.entry_idx is None or s.entry_dec is None:
        raise CarryStrategyError("exit attempted without an open position")
    s.cum_cost += s.exit_cost
    rt_cost = s.entry_cost + s.exit_cost
    s.trades.append(
        {
            "entry_decision_date": s.entry_dec,
            "entry_execution_date": s.rows[s.entry_idx]["date"],
            "exit_decision_date": order["decision_date"],
            "exit_execution_date": d,
            "status": "closed_proxy_round_trip",
            "gross_pnl": _round(s.trade_gross),
            "transaction_costs": rt_cost,
            "net_pnl": _round(s.trade_gross - rt_cost),
        }
    )
    s.pos = False
    s.entry_idx = s.entry_dec = None


def _exit_reason(s: _Sim, idx: int, d: str, tr_f: float | None, tr_b: float) -> str | None:
    if s.assign_start is not None and d >= s.assign_start:
        return "perpetual_execution_window"
    if s.basis is not None and s.basis[idx] >= tr_b:
        return "basis_reached_trailing_median"
    if tr_f is not None and tr_f <= 0:
        return "trailing_funding_non_positive"
    return "expiry_buffer" if d >= s.cutoff else None


def _queue_signal(s: _Sim, action: str, reason: str, idx: int, d: str, avail: bool) -> None:
    delay = s.cfg.delay
    s.signals.append({"date": d, "action": action, "reason": reason, "execution_available": avail})
    if not avail:
        return
    order = {"action": action, "execute_index": idx + delay, "decision_date": d}
    if delay == 0:
        _exec_order(s, order, idx, d)
    else:
        s.pending = order


def _step_signal(
    s: _Sim, idx: int, d: str, tr_f: float | None, tr_b: float, inc: float, fund: float
) -> tuple[float, float]:
    if s.pending is not None:
        return inc, fund
    if s.pos:
        reason = _exit_reason(s, idx, d, tr_f, tr_b)
        if not reason:
            return inc, fund
        avail = idx + s.cfg.delay < len(s.rows)
        if avail and s.cfg.delay == 0:
            lost = s.cfg.qty * s.funding[idx]
            inc, fund = inc - lost, 0.0
            s.cum_gross -= lost
            s.trade_gross -= lost
        _queue_signal(s, "exit", reason, idx, d, avail)
        return inc, fund
    blocked = d >= s.cutoff or (s.assign_start is not None and d >= s.assign_start)
    if blocked or tr_f is None or tr_f <= 0 or (s.basis is not None and s.basis[idx] > tr_b):
        return inc, fund
    reason = (
        "positive_trailing_funding"
        if s.cfg.variant == "funding_only"
        else "positive_trailing_funding_and_low_basis"
    )
    _queue_signal(s, "entry", reason, idx, d, idx + 1 < len(s.rows))
    return inc, fund


def _push_daily(
    s: _Sim,
    row: dict[str, Any],
    idx: int,
    d: str,
    pnls: tuple[float, float],
    bench: tuple[Callable[[str], dict[str, Any]] | None, dict[str, Any] | None],
) -> None:
    inc, fund = pnls
    base = {
        "date": d,
        "position": int(s.pos),
        "gross_pnl_increment": _round(inc),
        "near_settle": row.get("near_settle"),
        "far_settle": row.get("far_settle"),
        "daily_funding_included_rub": _round(fund),
    }
    if s.cfg.metrics_only:
        s.daily.append(base)
        return
    mark_fn, start_b = bench
    assert mark_fn is not None and start_b is not None
    b_row = mark_fn(d)
    s_val = s.cfg.capital + s.cum_gross - s.cum_cost
    b_val = s.cfg.capital * float(b_row["price"]) / float(start_b["price"])
    s.daily.append(
        {
            **base,
            "cumulative_gross_pnl": _round(s.cum_gross),
            "cumulative_transaction_costs": _round(s.cum_cost),
            "strategy_value_proxy": _round(s_val),
            "benchmark_value": _round(b_val),
            "X": _round(s_val - b_val),
            "benchmark_source_kind": b_row["source_kind"],
            "benchmark_source_ticker": b_row["source_ticker"],
            "benchmark_mark_kind": b_row["mark_kind"],
            "valuation_status": "EOD_settlement_proxy_not_executable_fill",
            "near_volume": row.get("near_volume"),
            "far_volume": row.get("far_volume"),
            "daily_basis_pnl_contract": float(
                row.get("daily_basis_pnl_short_near_long_far") or 0.0
            ),
            "daily_funding_contract": s.funding[idx],
        }
    )


def _window_stats(s: _Sim, idx: int, d: str) -> tuple[float | None, float]:
    w0 = idx - s.cfg.warmup
    tr_f = (
        statistics.fmean(s.funding[w0:idx])
        if all(s.known_on[j] < d for j in range(w0, idx))
        else None
    )
    return tr_f, (statistics.median(s.basis[w0:idx]) if s.basis else 0.0)


def _run_sim(
    s: _Sim, mark_fn: Callable[[str], dict[str, Any]] | None, start_b: dict[str, Any] | None
) -> None:
    for idx, row in enumerate(s.rows):
        d, inc, fund = str(row["date"]), 0.0, 0.0
        if s.pos and s.entry_idx is not None and idx > s.entry_idx:
            inc = s.cfg.qty * float(row["daily_gross_carry_pnl"])
            fund = s.cfg.qty * s.funding[idx]
            s.cum_gross += inc
            s.trade_gross += inc
        if s.pending is not None and s.pending["execute_index"] == idx:
            _exec_order(s, s.pending, idx, d)
            s.pending = None
        if idx >= s.cfg.warmup:
            tr_f, tr_b = _window_stats(s, idx, d)
            inc, fund = _step_signal(s, idx, d, tr_f, tr_b, inc, fund)
        _push_daily(s, row, idx, d, (inc, fund), (mark_fn, start_b))
    if s.pos and s.entry_idx is not None and s.entry_dec is not None:
        s.trades.append(
            {
                "entry_decision_date": s.entry_dec,
                "entry_execution_date": s.rows[s.entry_idx]["date"],
                "exit_decision_date": None,
                "exit_execution_date": None,
                "status": "open_at_data_end_no_executable_exit",
                "gross_pnl": _round(s.trade_gross),
                "transaction_costs": s.entry_cost,
                "net_pnl": _round(s.trade_gross - s.entry_cost),
            }
        )


def _finalize_daily(
    s: _Sim, margin_leg_sum: float, mark_fn: Callable[[str], dict[str, Any]]
) -> int:
    shortfalls = 0
    if s.cfg.cash_in_lqdt:
        margin = margin_leg_sum * s.cfg.qty
        vals, shortfalls = lqdt_overlay_values(
            s.cfg.capital,
            [r["cumulative_gross_pnl"] - r["cumulative_transaction_costs"] for r in s.daily],
            [margin if r["position"] else 0.0 for r in s.daily],
            [float(mark_fn(r["date"])["price"]) for r in s.daily],
        )
        for r, val in zip(s.daily, vals, strict=True):
            r["strategy_value_proxy"], r["X"] = _round(val), _round(val - r["benchmark_value"])
    peak = 0.0
    for r in s.daily:
        peak = max(peak, float(r["X"]))
        r["H"], r["D"] = _round(peak), _round(peak - float(r["X"]))
    return shortfalls


def _build_report(
    s: _Sim,
    leg_info: tuple[str, str, str, str, dict[str, dict[str, Any]], str, Path],
    run_info: tuple[Path, str, Callable[[str], dict[str, Any]], dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    near, far, far_exp, assign_status, contracts, contracts_hash, contracts_path = leg_info
    bench_path, bench_hash, mark_fn, cost_prof, risk = run_info
    cfg, w_obs = s.cfg, s.cfg.warmup
    margin_sum = float(contracts[near]["initial_margin"]) + float(contracts[far]["initial_margin"])
    shortfalls = _finalize_daily(s, margin_sum, mark_fn)
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
            "cash_shortfall_rows": shortfalls,
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


def build_carry_strategy(
    carry_result: dict[str, Any], benchmark_path: Path, contracts_path: Path, **options: Any
) -> dict[str, Any]:
    """Evaluate a frozen carry hypothesis on settlement proxies."""
    cfg = _parse_cfg(options)
    if carry_result.get("schema_version") != CARRY_SCHEMA:
        raise CarryStrategyError("unsupported carry decomposition schema")
    raw_rows = carry_result.get("rows")
    if not isinstance(raw_rows, list):
        raise CarryStrategyError("carry decomposition does not contain the required warmup")
    rows = cast(list[dict[str, Any]], raw_rows)
    if len(rows) <= cfg.warmup:
        raise CarryStrategyError("carry decomposition does not contain the required warmup")
    near, far = carry_result.get("near"), carry_result.get("far")
    if not isinstance(near, str) or not isinstance(far, str):
        raise CarryStrategyError("carry contract identities are required")
    contracts, contracts_hash = load_contracts(contracts_path, near, far)
    far_exp = contracts[far].get("expiry")
    cutoff, assign_start, assign_status = _expiry_windows(
        rows, far, far_exp, cfg.buf_days, cfg.assign_days
    )
    cost_prof = _cost_profile(
        cfg.cost_scenario, cfg.slip_ticks, contracts[near], contracts[far], rows[-1]
    )
    sim = _Sim(
        cfg=cfg,
        rows=rows,
        entry_cost=float(cost_prof["entry_cost"]) * cfg.qty,
        exit_cost=float(cost_prof["exit_cost"]) * cfg.qty,
        cutoff=cutoff,
        assign_start=assign_start,
        funding=[float(r["daily_funding_received_short_near"]) for r in rows],
        known_on=[str(r.get("funding_known_on", "")) for r in rows],
        basis=[float(r["basis_far_minus_near"]) for r in rows]
        if cfg.variant == "basis_median"
        else None,
    )
    mark_fn, start_b, bench_hash = (
        (None, None, "")
        if cfg.metrics_only
        else _benchmark_lookup(benchmark_path, str(rows[0]["date"]))
    )
    _run_sim(sim, mark_fn, start_b)
    if cfg.metrics_only:
        return {"trades": sim.trades, "signals": sim.signals, "daily": sim.daily}
    risk = assess_vtb_pair(
        contracts_path,
        near,
        far,
        initial_capital=cfg.capital,
        observed_daily_pnl=[float(r["daily_gross_carry_pnl"]) for r in rows[1:]],
        quantity=cfg.qty,
    )
    return _build_report(
        sim,
        (near, far, str(far_exp), assign_status, contracts, contracts_hash, contracts_path),
        (
            benchmark_path,
            bench_hash,
            cast(Callable[[str], dict[str, Any]], mark_fn),
            cost_prof,
            risk,
        ),
    )
