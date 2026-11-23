"""Benchmark alignment and transaction cost profiles for carry strategy evaluation."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.accounting.costs import side_cost, tariff_profile
from moex_spread_lab.core.hashed_json_cache import HashedJSONCache as _HashedJSONCache
from moex_spread_lab.core.w0 import W0Error, read_hashed_json

from .study import CarryStrategyError, _round

_BENCHMARK_PARSED_CACHE: _HashedJSONCache[
    tuple[str, int, int], tuple[dict[str, dict[str, Any]], str]
] = _HashedJSONCache(64)


def _benchmark_rows(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    resolved = path.resolve()
    try:
        stat = resolved.stat()
    except OSError as exc:
        raise CarryStrategyError(f"cannot read LQDT benchmark {path}: {exc}") from exc
    key = (str(resolved), stat.st_mtime_ns, stat.st_size)
    cached = _BENCHMARK_PARSED_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        raw_payload, digest = read_hashed_json(
            resolved, error=CarryStrategyError, label="LQDT benchmark"
        )
    except CarryStrategyError:
        raise
    except W0Error as exc:
        raise CarryStrategyError(str(exc)) from exc
    payload = cast(Any, raw_payload)
    if (
        not isinstance(payload, dict)
        or cast(dict[str, Any], payload).get("schema_version") != "lqdt-lineage.v1"
    ):
        raise CarryStrategyError("unsupported LQDT benchmark schema")
    rows: dict[str, dict[str, Any]] = {}
    for raw_row in cast(dict[str, Any], payload).get("rows", []):
        if not isinstance(raw_row, dict) or not isinstance(
            cast(dict[str, Any], raw_row).get("date"), str
        ):
            raise CarryStrategyError("malformed LQDT benchmark row")
        row = cast(dict[str, Any], raw_row)
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
    result = (rows, digest)
    _BENCHMARK_PARSED_CACHE.put(key, result)
    return result


def make_benchmark_lookup(
    benchmark_path: Path, first_date: str
) -> tuple[Callable[[str], dict[str, Any]], dict[str, Any], str]:
    benchmark, benchmark_hash = _benchmark_rows(benchmark_path)
    benchmark_dates = sorted(benchmark)
    if not benchmark_dates:
        raise CarryStrategyError("benchmark has no dates")

    def benchmark_mark(date_key: str) -> dict[str, Any]:
        index = bisect_left(benchmark_dates, date_key)
        if index < len(benchmark_dates) and benchmark_dates[index] == date_key:
            return {**benchmark[date_key], "mark_kind": "daily_close_same_date"}
        if index == 0:
            raise CarryStrategyError(f"benchmark has no mark on or before {date_key}")
        prior = benchmark[benchmark_dates[index - 1]]
        return {**prior, "mark_kind": "prior_daily_close"}

    return benchmark_mark, benchmark_mark(first_date), benchmark_hash


def _transaction_cost_profile(
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
        near_side = near_price * near_lot_size * rate
        far_side = far_price * far_lot_size * rate
        round_trip = 2 * (near_side + far_side)
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
            "entry_cost": _round(near_side + far_side),
            "exit_cost": _round(near_side + far_side),
            "round_trip_cost": _round(round_trip),
            "status": (
                "current_moex_rate_plus_user_confirmed_vtb_zero_derivatives_broker_fee"
                if name == "vtb_online_prime_current"
                else "current_official_moex_taker_rate_account_broker_unverified"
            ),
            "source": ["https://www.moex.com/s93", "docs/inputs/moy_online_praym.pdf"],
            "funding_component": "actual_SWAPRATE_included;_broker_and_other_costs_not_included",
        }
    profiles: dict[str, dict[str, Any]] = {
        "low": {
            "exchange": 0.5,
            "clearing": 0.5,
            "broker": 0.5,
            "slippage_ticks": 0.5,
            "exit": 0.5,
        },
        "base": {
            "exchange": 1.0,
            "clearing": 1.0,
            "broker": 1.0,
            "slippage_ticks": 1.0,
            "exit": 1.0,
        },
        "stress": {
            "exchange": 2.0,
            "clearing": 2.0,
            "broker": 2.0,
            "slippage_ticks": 2.0,
            "exit": 2.0,
        },
    }
    try:
        values = profiles[name].copy()
    except KeyError as exc:
        raise CarryStrategyError(f"unknown transaction cost scenario: {name}") from exc
    resolved_far_tv = tick_value if far_tick_value is None else far_tick_value
    leg_base = float(values["exchange"]) + float(values["clearing"]) + float(values["broker"])
    slip = float(values["slippage_ticks"]) * (tick_value + resolved_far_tv)
    values["entry_cost"] = round(2 * leg_base + slip, 8)
    values["exit_cost"] = round(2 * leg_base + slip + 2 * float(values["exit"]), 8)
    values["round_trip_cost"] = round(float(values["entry_cost"]) + float(values["exit_cost"]), 8)
    values["scenario"] = name
    values["funding_component"] = "actual_SWAPRATE_included;_illustrative_funding_cost_not_added"
    values["status"] = "illustrative_unverified"
    return values


def resolve_carry_cost_profile(
    scenario: str,
    slippage_ticks: float,
    near_contract: dict[str, Any],
    far_contract: dict[str, Any],
    last_row: dict[str, Any],
) -> dict[str, Any]:
    near_ref = float(last_row["near_settle"]) if "near_settle" in last_row else None
    far_ref = float(last_row["far_settle"]) if "far_settle" in last_row else None
    if scenario == "tariff":
        if near_ref is None or far_ref is None:
            raise CarryStrategyError("tariff scenario requires settlement prices")
        tariff = tariff_profile(slippage_ticks)
        side = sum(
            side_cost(
                tariff,
                [(near_contract, near_ref), (far_contract, far_ref)],
                1,
            ).values()
        )
        return {
            **tariff,
            "entry_cost": round(side, 8),
            "exit_cost": round(side, 8),
            "round_trip_cost": round(2 * side, 8),
            "near_reference_price": near_ref,
            "far_reference_price": far_ref,
        }
    return _transaction_cost_profile(
        scenario,
        float(near_contract["tick_value"]),
        near_price=near_ref,
        far_price=far_ref,
        near_lot_size=int(near_contract["lot_size"]),
        far_lot_size=int(far_contract["lot_size"]),
        far_tick_value=float(far_contract["tick_value"]),
    )
