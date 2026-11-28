"""Frozen H2/H3 carry hypothesis simulation, LQDT benchmark alignment, and report assembly."""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.accounting.account_risk import assess_vtb_pair
from moex_spread_lab.core.derived_cache import HashedJSONCache
from moex_spread_lab.core.w0 import W0Error, load_contracts, read_hashed_json

from .report import build_carry_report, build_cost_profile, transaction_cost_profile
from .study import CARRY_SCHEMA, CarryStrategyError, _round

__all__ = ["build_carry_strategy", "transaction_cost_profile"]

_BENCHMARK_CACHE: HashedJSONCache[tuple[str, int, int], tuple[dict[str, dict[str, Any]], str]] = (
    HashedJSONCache(64)
)
_TRADE_KEYS = "entry_decision_date entry_execution_date exit_decision_date exit_execution_date status gross_pnl transaction_costs net_pnl"


def _kv(keys: str, values: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(keys.split(), values, strict=True))


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
    if tr_f is not None and (not math.isfinite(tr_f) or tr_f <= 0):
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
    if (
        blocked
        or tr_f is None
        or not math.isfinite(tr_f)
        or tr_f <= 0
        or (s.basis is not None and s.basis[idx] > tr_b)
    ):
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
            _kv(
                _TRADE_KEYS,
                (
                    s.entry_dec,
                    s.rows[s.entry_idx]["date"],
                    None,
                    None,
                    "open_at_data_end_no_executable_exit",
                    _round(s.trade_gross),
                    s.entry_cost,
                    _round(s.trade_gross - s.entry_cost),
                ),
            )
        )


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
    cost_prof = build_cost_profile(
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
    return build_carry_report(
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
