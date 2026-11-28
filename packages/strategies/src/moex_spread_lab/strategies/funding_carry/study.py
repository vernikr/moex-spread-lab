"""Read-only MOEX EOD history normalization and funding-aware carry decomposition."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from moex_spread_lab.core.w0 import load_contracts
from moex_spread_lab.data.iss.catalog import (
    HISTORY_FIELDS,
    HISTORY_SCHEMA,
    IssHistoryError,
)
from moex_spread_lab.data.iss.catalog import fetch_history as _iss_fetch_history

CARRY_SCHEMA = "carry-study.v1"
STRATEGY_SCHEMA = "carry-strategy.v1"
FUNDING_REGIMES: tuple[dict[str, Any], ...] = (
    {
        "id": "cnyrubf-k1-003-k2-035",
        "effective_from": "2023-08-22",
        "k1": 0.0003,
        "k2": 0.0035,
        "source": "MOEX notice n63166 and perpetual-futures page",
    },
    {
        "id": "cnyrubf-k1-000-k2-035",
        "effective_from": "2026-01-19",
        "k1": 0.0,
        "k2": 0.0035,
        "source": "MOEX notice n96856 and perpetual-futures page",
    },
)


class CarryError(IssHistoryError):
    """Raised when EOD carry inputs fail the funding-aware study contract."""


class CarryStrategyError(CarryError):
    """Raised when a frozen carry strategy cannot be evaluated safely."""


def _round(value: float) -> float:
    return round(float(value), 8)


def _funding_regime(date_key: str, regimes: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    if not regimes:
        return None
    regime = regimes[0]
    for candidate in regimes:
        if date_key >= candidate["effective_from"]:
            regime = candidate
    return regime


def fetch_history(
    secid: str,
    from_date: str,
    till_date: str,
    **options: Any,
) -> dict[str, Any]:
    return _iss_fetch_history(
        secid,
        from_date,
        till_date,
        cache_dir=options.get("cache_dir"),
        delay_seconds=float(options.get("delay_seconds", 0.1)),
        timeout=float(options.get("timeout", 30.0)),
        opener=options.get("opener", urlopen),
        fields=options.get("fields", HISTORY_FIELDS),
        allow_empty=bool(options.get("allow_empty", False)),
        cache_only=bool(options.get("cache_only", False)),
        error=CarryError,
    )


def _parse_history_row(row: Mapping[str, Any], secid: str) -> tuple[float, float | None]:
    raw_settle = row["SETTLEPRICE"]
    raw_swap = row.get("SWAPRATE")
    try:
        settle = float(raw_settle)
        swaprate = float(raw_swap) if raw_swap is not None else None
    except (TypeError, ValueError) as exc:
        raise CarryError(f"non-numeric history field for {secid} {row.get('TRADEDATE')}") from exc
    if isinstance(raw_settle, bool) or not math.isfinite(settle) or settle <= 0:
        raise CarryError(f"Некорректная расчётная цена {secid} за {row['TRADEDATE']}.")
    if swaprate is not None and (isinstance(raw_swap, bool) or not math.isfinite(swaprate)):
        raise CarryError(f"Некорректная ставка SWAPRATE {secid} за {row['TRADEDATE']}.")
    return settle, swaprate


def _normalize_history(
    source: dict[str, Any], secid: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if source.get("schema_version") != HISTORY_SCHEMA or source.get("secid") != secid:
        raise CarryError(f"unsupported history input for {secid}")
    normalized: dict[str, dict[str, Any]] = {}
    for row in source.get("rows", []):
        if row.get("TRADEDATE") is None or row.get("SETTLEPRICE") is None:
            continue
        settle, swaprate = _parse_history_row(row, secid)
        trade_date = str(row["TRADEDATE"])
        normalized[trade_date] = {
            "date": trade_date,
            "settle": settle,
            "swaprate": swaprate,
            "open_position": row.get("OPENPOSITION"),
            "volume": row.get("VOLUME"),
            "value": row.get("VALUE"),
        }
    if len(normalized) < 2:
        raise CarryError(f"fewer than two settlement rows for {secid}")
    return normalized, {
        "secid": secid,
        "source": source.get("source", {}),
        "row_count": len(normalized),
        "first_date": min(normalized),
        "last_date": max(normalized),
    }


def _cap_check(
    date_key: str,
    previous_date: str | None,
    swaprate: float,
    near_rows: dict[str, dict[str, Any]],
    regimes: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any] | None, float | None, str]:
    regime = _funding_regime(date_key, regimes)
    cap_per_unit = (
        None
        if previous_date is None or regime is None or regime.get("k2") is None
        else float(regime["k2"]) * float(near_rows[previous_date]["settle"])
    )
    if cap_per_unit is not None:
        status = (
            "within_rounded_k2_cap"
            if abs(swaprate) <= cap_per_unit + 0.00001
            else "above_k2_cap_review_required"
        )
    else:
        status = "not_modelled" if regime is None else "not_applicable_initial_observation"
    return regime, cap_per_unit, status


def _basis_pnl(
    date_key: str,
    previous_date: str | None,
    legs: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]],
    multipliers: tuple[float, float],
) -> float:
    if previous_date is None:
        return 0.0
    near_rows, far_rows = legs
    near_mult, far_mult = multipliers
    near_change = near_rows[date_key]["settle"] - near_rows[previous_date]["settle"]
    far_change = far_rows[date_key]["settle"] - far_rows[previous_date]["settle"]
    return far_change * far_mult - near_change * near_mult


def _swaprate_for(date_key: str, near: str, near_row: Mapping[str, Any], mark: Any) -> float:
    raw = mark.value if mark else near_row["swaprate"]
    if raw is None or isinstance(raw, bool):
        raise CarryError(f"missing {near} SWAPRATE on {date_key}")
    val = float(raw)
    if not math.isfinite(val):
        raise CarryError(f"non-finite {near} SWAPRATE on {date_key}")
    return val


def _regime_fields(
    date_key: str,
    previous_date: str | None,
    swaprate: float,
    near_rows: dict[str, dict[str, Any]],
    regimes: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    regime, cap_per_unit, cap_status = _cap_check(
        date_key, previous_date, swaprate, near_rows, regimes
    )
    return {
        "funding_regime_id": regime["id"] if regime else "not_modelled",
        "funding_k1": regime.get("k1") if regime else None,
        "funding_k2": regime.get("k2") if regime else None,
        "funding_cap_per_unit": _round(cap_per_unit) if cap_per_unit is not None else None,
        "funding_cap_status": cap_status,
    }


def _build_study_rows(
    common_dates: list[str],
    near: str,
    legs: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]],
    mults: tuple[float, float, float],
    regimes: tuple[dict[str, Any], ...],
    funding_marks: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    near_rows, far_rows = legs
    near_lot_size, near_mult, far_mult = mults
    rows: list[dict[str, Any]] = []
    cumulative_mark = 0.0
    cumulative_funding = 0.0
    previous_date: str | None = None
    for date_key in common_dates:
        near_row, far_row = near_rows[date_key], far_rows[date_key]
        mark = funding_marks[date_key] if funding_marks is not None else None
        swaprate = _swaprate_for(date_key, near, near_row, mark)
        daily_basis_pnl = _basis_pnl(date_key, previous_date, legs, (near_mult, far_mult))
        funding_received = 0.0 if previous_date is None else swaprate * near_lot_size
        regime_fields = _regime_fields(date_key, previous_date, swaprate, near_rows, regimes)
        cumulative_mark += daily_basis_pnl
        cumulative_funding += funding_received
        rows.append(
            {
                "date": date_key,
                "near_settle": near_row["settle"],
                "near_swaprate": swaprate,
                **({"funding_known_on": mark.right} if mark and mark.kind == "midpoint" else {}),
                "near_lot_size": near_lot_size,
                "far_settle": far_row["settle"],
                "basis_far_minus_near": _round(far_row["settle"] - near_row["settle"]),
                "daily_basis_pnl_short_near_long_far": _round(daily_basis_pnl),
                "daily_funding_received_short_near": _round(funding_received),
                "daily_gross_carry_pnl": _round(daily_basis_pnl + funding_received),
                **regime_fields,
                "cumulative_basis_pnl": _round(cumulative_mark),
                "cumulative_funding_received": _round(cumulative_funding),
                "cumulative_gross_carry_pnl": _round(cumulative_mark + cumulative_funding),
                "swaprate_source_unit": "RUB_per_contract_day_assumed_from_R06_and_ISS_history",
                "near_volume": near_row.get("volume"),
                "far_volume": far_row.get("volume"),
            }
        )
        previous_date = date_key
    return rows


def build_carry_study(
    near_source: dict[str, Any],
    far_source: dict[str, Any],
    contracts_path: Path,
    **options: Any,
) -> dict[str, Any]:
    allowed = {
        "near",
        "far",
        "funding_regimes",
        "funding_marks",
        "no_clearing_dates",
        "through",
    }
    unknown = set(options) - allowed
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {sorted(unknown)}")
    near = str(options.get("near", "CNYRUBF"))
    far = str(options.get("far", "CRZ6"))
    funding_regimes: tuple[dict[str, Any], ...] | None = options.get("funding_regimes")
    funding_marks: Mapping[str, Any] | None = options.get("funding_marks")
    no_clearing_dates: Iterable[str] = options.get("no_clearing_dates", ())
    through: str | None = options.get("through")
    near_rows, near_provenance = _normalize_history(near_source, near)
    far_rows, far_provenance = _normalize_history(far_source, far)
    contracts, contracts_hash = load_contracts(contracts_path, near, far)
    near_contract, far_contract = contracts[near], contracts[far]
    common_dates = sorted((set(near_rows) & set(far_rows)) - set(no_clearing_dates))
    if through is not None:
        common_dates = [d for d in common_dates if d <= through]
    if len(common_dates) < 2:
        raise CarryError("fewer than two common EOD settlement dates")
    near_mult = float(near_contract["tick_value"]) / float(near_contract["tick_size"])
    far_mult = float(far_contract["tick_value"]) / float(far_contract["tick_size"])
    regimes = (
        FUNDING_REGIMES
        if funding_regimes is None and near == "CNYRUBF"
        else (funding_regimes or ())
    )
    rows = _build_study_rows(
        common_dates,
        near,
        (near_rows, far_rows),
        (float(near_contract["lot_size"]), near_mult, far_mult),
        regimes,
        funding_marks,
    )
    funding_values = [float(row["daily_funding_received_short_near"]) for row in rows[1:]]
    return {
        "schema_version": CARRY_SCHEMA,
        "release_state": "exploratory_inconclusive",
        "study_role": "funding_aware_carry_decomposition_not_execution_backtest",
        "near": near,
        "far": far,
        "direction": "short_near_long_far",
        "price_role": "EOD_SETTLEPRICE",
        "funding_role": f"{near}_SWAPRATE_received_by_short_near",
        "rows": rows,
        "summary": {
            "common_row_count": len(rows),
            "first_date": rows[0]["date"],
            "last_date": rows[-1]["date"],
            "total_basis_pnl": rows[-1]["cumulative_basis_pnl"],
            "total_funding_received": rows[-1]["cumulative_funding_received"],
            "total_gross_carry_pnl": rows[-1]["cumulative_gross_carry_pnl"],
            "mean_daily_funding_received": _round(sum(funding_values) / len(funding_values)),
            "positive_funding_rows": sum(value > 0 for value in funding_values),
            "negative_funding_rows": sum(value < 0 for value in funding_values),
            "zero_funding_rows": sum(value == 0 for value in funding_values),
            "funding_regime_counts": {
                regime["id"]: sum(row["funding_regime_id"] == regime["id"] for row in rows)
                for regime in regimes
            },
            "funding_cap_review_rows": sum(
                row["funding_cap_status"] == "above_k2_cap_review_required" for row in rows
            ),
            "transaction_costs_included": False,
            "historical_executable_fills": False,
        },
        "economics": {
            "near_lot_size": near_contract["lot_size"],
            "far_lot_size": far_contract["lot_size"],
            "tick_size": near_contract["tick_size"],
            "tick_value_rub": near_contract["tick_value"],
            "near_tick_size": near_contract["tick_size"],
            "near_tick_value_rub": near_contract["tick_value"],
            "far_tick_size": far_contract["tick_size"],
            "far_tick_value_rub": far_contract["tick_value"],
            "near_rub_per_price_unit": _round(near_mult),
            "far_rub_per_price_unit": _round(far_mult),
            "rub_per_price_unit": _round(near_mult),
            "historical_specification_status": "current_snapshot_fallback_assumed",
            "fixing_mismatch_risk": "CNYRUBF_TOM_vs_CRZ6_CNYFIXME",
            "funding_regimes": list(regimes),
        },
        "provenance": {
            "near_history": near_provenance,
            "far_history": far_provenance,
            "contracts_sha256": contracts_hash,
            "contracts_path": str(contracts_path),
        },
    }


__all__ = [
    "CARRY_SCHEMA",
    "FUNDING_REGIMES",
    "STRATEGY_SCHEMA",
    "CarryError",
    "CarryStrategyError",
    "_funding_regime",
    "_normalize_history",
    "_round",
    "build_carry_study",
    "fetch_history",
]
