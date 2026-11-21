"""Offline screening of documented MOEX futures-spread candidates."""

from __future__ import annotations

from bisect import bisect_left
from functools import partial
from itertools import pairwise
from pathlib import Path
from typing import Any

from .account_risk import assess_vtb_pair
from .analytics import get_cost_scenario, load_contracts, run_mean_reversion_baseline
from .data_lake import BarSeries, DataLakeError, LegacyJsonBarStore
from .timeframes import get_timeframe, timeframe_for_interval
from .universe import SCREEN_PAIRS
from .w0 import W0Error, read_hashed_json

SCREEN_SCHEMA = "pair-screen.v1"


class ScreenError(W0Error):
    """Raised when the offline pair screen cannot be built."""


def _round(value: float) -> float:
    return round(float(value), 8)


_read_json = partial(read_hashed_json, error=ScreenError, label="screen input")


def _bar_series(source: BarSeries | Path) -> BarSeries:
    if isinstance(source, BarSeries):
        return source
    if isinstance(source, Path):
        try:
            return LegacyJsonBarStore().read(source)
        except DataLakeError as exc:
            raise ScreenError(str(exc)) from exc
    raise ScreenError("screen candle source must be a BarSeries or Path")


def _basis_daily_pnl(series: dict[str, Any]) -> list[float]:
    rows = series["rows"]
    near = series["contracts"][series["near"]]
    far = series["contracts"][series["far"]]
    near_multiplier = float(near["tick_value"]) / float(near["tick_size"])
    far_multiplier = float(far["tick_value"]) / float(far["tick_size"])
    result = []
    for previous, current in pairwise(rows):
        result.append(
            (float(current["far_close"]) - float(previous["far_close"])) * far_multiplier
            - (float(current["near_close"]) - float(previous["near_close"])) * near_multiplier
        )
    return result


_NORMALIZED_BY_TS_CACHE: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}


def _canonical_timestamp(row: dict[str, Any]) -> str | None:
    value = row.get("begin")
    if not isinstance(value, str) or len(value) < 10:
        return None
    return value[:19].replace(" ", "T") if len(value) >= 19 else value[:10]


def _rows_by_ts(series: BarSeries) -> dict[str, dict[str, Any]]:
    cached = _NORMALIZED_BY_TS_CACHE.get(series.cache_key)
    if cached is not None:
        return cached
    mapped = {
        ts: row
        for row in series.rows
        if (ts := _canonical_timestamp(row)) and row.get("close") is not None
    }
    if len(_NORMALIZED_BY_TS_CACHE) > 1024:
        _NORMALIZED_BY_TS_CACHE.clear()
    _NORMALIZED_BY_TS_CACHE[series.cache_key] = mapped
    return mapped


def _normalized_series(
    near_source: BarSeries | Path,
    far_source: BarSeries | Path,
    benchmark_path: Path,
    contracts_path: Path,
    *,
    near: str,
    far: str,
    prior_benchmark_dates=(),
    timeframe: str | None = None,
) -> dict[str, Any]:
    near_series = _bar_series(near_source)
    far_series = _bar_series(far_source)
    benchmark_payload, benchmark_hash = _read_json(benchmark_path)
    if near_series.schema_version != "iss-candles.v1" or far_series.schema_version != "iss-candles.v1":
        raise ScreenError("screen requires iss-candles.v1 pair inputs")
    if benchmark_payload.get("schema_version") != "lqdt-lineage.v1":
        raise ScreenError("screen requires lqdt-lineage.v1 benchmark")
    try:
        spec = get_timeframe(timeframe) if timeframe is not None else timeframe_for_interval(near_series.interval)
    except ValueError as exc:
        raise ScreenError(str(exc)) from exc
    if near_series.interval != spec.iss_interval or far_series.interval != spec.iss_interval:
        raise ScreenError(f"candle interval mismatch: expected {spec.iss_interval} ({spec.id})")
    contracts, contracts_hash = load_contracts(contracts_path, near, far)
    near_contract = contracts[near]
    far_contract = contracts[far]
    near_multiplier = float(near_contract["tick_value"]) / float(near_contract["tick_size"])
    far_multiplier = float(far_contract["tick_value"]) / float(far_contract["tick_size"])
    common_tick_rub = max(float(near_contract["tick_value"]), float(far_contract["tick_value"]), 1.0)

    near_rows = _rows_by_ts(near_series)
    far_rows = _rows_by_ts(far_series)
    benchmark = {row["date"]: row for row in benchmark_payload.get("rows", []) if row.get("date")}
    observed = sorted(benchmark)
    rows = []
    for timestamp in sorted(set(near_rows) & set(far_rows)):
        session_date = timestamp[:10]
        benchmark_date = session_date
        benchmark_row = benchmark.get(session_date)
        mark_kind = "daily_close_same_date"
        if spec.intraday or (benchmark_row is None and session_date in prior_benchmark_dates):
            prior_index = bisect_left(observed, session_date) - 1
            benchmark_date = observed[prior_index] if prior_index >= 0 else None
            benchmark_row = benchmark.get(benchmark_date) if benchmark_date else None
            mark_kind = "prior_daily_close"
        if benchmark_row is None:
            continue
        near_row, far_row = near_rows[timestamp], far_rows[timestamp]
        near_close, far_close = float(near_row["close"]), float(far_row["close"])
        normalized_spread = near_close * near_multiplier - far_close * far_multiplier
        rows.append({
            "date": timestamp if spec.intraday else session_date,
            "session_date": session_date,
            "timestamp": timestamp,
            "near_close": near_close,
            "far_close": far_close,
            "near_open": float(near_row.get("open") or near_close),
            "far_open": float(far_row.get("open") or far_close),
            "spread_value": _round(normalized_spread),
            "spread_ticks": _round(normalized_spread / common_tick_rub),
            "benchmark_price": float(benchmark_row["price"]),
            "benchmark_source_ticker": benchmark_row.get("source_ticker"),
            "benchmark_source_kind": benchmark_row.get("source_kind"),
            "benchmark_mark_kind": mark_kind,
            "benchmark_mark_date": benchmark_date,
            "near_price_proxy_kind": near_row.get("price_proxy_kind", "candle_close"),
            "far_price_proxy_kind": far_row.get("price_proxy_kind", "candle_close"),
            "near_volume": near_row.get("volume"),
            "far_volume": far_row.get("volume"),
        })
    if len(rows) < 2:
        raise ScreenError(f"fewer than two normalized common rows for {near}/{far}")
    return {
        "schema_version": "analytical-series.v1",
        "near": near,
        "far": far,
        "interval": spec.iss_interval,
        "timeframe": spec.id,
        "direction": "near_minus_far_normalized_rub",
        "price_unit": "rub_normalized_per_pair",
        "spread_tick_value_rub": common_tick_rub,
        "rows": rows,
        "contracts": contracts,
        "provenance": {
            "near_candles_sha256": near_series.source_sha256,
            "far_candles_sha256": far_series.source_sha256,
            "benchmark_sha256": benchmark_hash,
            "contracts_sha256": contracts_hash,
            "near_candles_path": near_series.source_path,
            "far_candles_path": far_series.source_path,
            "benchmark_path": str(benchmark_path),
            "contracts_path": str(contracts_path),
            "normalization": "close * (tick_value / tick_size) per leg",
            "benchmark_alignment": "previous observed daily close for intraday bars" if spec.intraday else "same-day close; documented non-clearing fallback only",
        },
    }


def screen_pairs(
    candles_dir: Path,
    benchmark_path: Path,
    contracts_path: Path,
    *,
    initial_capital: float = 30_000.0,
    pairs: tuple[dict[str, str], ...] = SCREEN_PAIRS,
) -> dict[str, Any]:
    """Run the same analytical control and account stress screen for each pair."""
    if initial_capital <= 0:
        raise ScreenError("initial capital must be positive")
    contracts, contracts_hash = _read_json(contracts_path)
    if contracts.get("schema_version") != "iss-intake.v1":
        raise ScreenError("screen requires iss-intake.v1 contracts")
    benchmark, benchmark_hash = _read_json(benchmark_path)
    if benchmark.get("schema_version") != "lqdt-lineage.v1":
        raise ScreenError("screen requires lqdt-lineage.v1 benchmark")
    stress_cost = get_cost_scenario("stress")
    candidates: list[dict[str, Any]] = []
    for pair in pairs:
        near = pair["near"]
        far = pair["far"]
        near_path = candles_dir / f"{near}_1d.json"
        far_path = candles_dir / f"{far}_1d.json"
        base = {**pair, "price_role": "1d_candle_close_proxy", "candidate_status": "screened"}
        try:
            series = _normalized_series(
                near_path,
                far_path,
                benchmark_path,
                contracts_path,
                near=near,
                far=far,
            )
            risk = assess_vtb_pair(
                contracts_path,
                near,
                far,
                initial_capital=initial_capital,
                observed_daily_pnl=_basis_daily_pnl(series),
            )
            baseline = run_mean_reversion_baseline(
                series,
                initial_capital=initial_capital,
                analysis_role="diagnostic_only",
                cost_profile=stress_cost,
            )
            summary = baseline["summary"]
            base.update({
                "common_rows": summary["common_row_count"],
                "first_date": summary["first_date"],
                "last_date": summary["last_date"],
                "pair_initial_margin_rub": risk["pair"]["pair_margin_no_offset_rub"],
                "stress_screen_passed": risk["summary"]["all_scenarios_passed"],
                "current_margin_screen_passed": risk["summary"]["current_snapshot_passed"],
                "stress_remaining_capital_rub": risk["summary"]["most_severe_remaining_capital_rub"],
                "h1_control": {
                    "trade_count": summary["trade_count"],
                    "gross_pnl_rub": summary["gross_pnl"],
                    "illustrative_stress_costs_rub": summary["modeled_costs"],
                    "final_X_rub": summary["final_X"],
                    "max_drawdown_rub": summary["max_drawdown_D"],
                },
                "decision": (
                    "candidate_for_separate_hypothesis"
                    if risk["summary"]["all_scenarios_passed"] and summary["final_X"] > 0
                    else "not_promoted"
                ),
                "risk": risk,
                "series_provenance": series["provenance"],
            })
        except (OSError, ScreenError, W0Error) as exc:
            base.update({
                "candidate_status": "rejected_input_or_capital_gate",
                "decision": "not_screenable",
                "error": str(exc),
            })
        candidates.append(base)
    candidates.sort(
        key=lambda item: (
            item.get("decision") != "candidate_for_separate_hypothesis",
            -(item.get("h1_control", {}).get("final_X_rub") or float("-inf")),
        )
    )
    for index, candidate in enumerate(candidates, start=1):
        candidate["rank"] = index
    return {
        "schema_version": SCREEN_SCHEMA,
        "release_state": "exploratory_inconclusive",
        "screen_id": "documented_flagship_pairs_h1_control",
        "method": {
            "hypothesis": "fixed_mean_reversion_control_only",
            "lookback": 20,
            "entry_z": 2.0,
            "exit_z": 0.5,
            "decision_delay_bars": 1,
            "cost_profile": "illustrative_stress_unverified",
            "risk_profile": "vtb_ebs_kpur_working_profile",
            "price_proxy": "daily_candle_close",
            "historical_order_book": False,
        },
        "summary": {
            "candidate_count": len(candidates),
            "stress_pass_count": sum(item.get("stress_screen_passed", False) for item in candidates),
            "positive_h1_control_count": sum(
                (item.get("h1_control", {}).get("final_X_rub") or 0) > 0 for item in candidates
            ),
            "separate_hypothesis_candidates": sum(
                item.get("decision") == "candidate_for_separate_hypothesis" for item in candidates
            ),
        },
        "candidates": candidates,
        "provenance": {
            "contracts_path": str(contracts_path),
            "contracts_sha256": contracts_hash,
            "benchmark_path": str(benchmark_path),
            "benchmark_sha256": benchmark_hash,
            "candidate_source": "R06_documented_flagship_same_asset_pairs",
        },
    }
