"""Payload shapes emitted by the mean-reversion engine.

The tuning search consumes the price-only intent payload, the sizing pass consumes the
metrics payload and the web UI consumes the full `execution-proof.v1` payload.
"""

from __future__ import annotations

from math import fsum
from typing import Any

from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import PAIR_UNIVERSE

from .ledger_blocks import price_ledger
from .mean_reversion_blocks import (
    holding_flags,
    instrument_for,
    notes,
    selection_for,
    series_rows_for,
    side,
    stamp,
    trades_for,
    window_meta,
)
from .mean_reversion_engine import report_for
from .mean_reversion_engine import resolve_window as _resolve_window
from .mean_reversion_spec import MeanReversionSpec, MeanReversionWindow
from .pair_payload import PairPayloadSpec, pair_payload
from .payload_tools import FORCED_EXIT_REASONS, PROOF_SCHEMA, positive_pair_volume


def intent_trades(window: MeanReversionWindow, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Intent-level trades for the sizing pass (prices only, no cost or capital data)."""
    intraday = get_timeframe(window.series["timeframe"]).intraday
    trades: list[dict[str, Any]] = []
    for row in report["trades"]:
        entry_date = stamp(row["entry_date"], intraday)
        exit_date = stamp(row["exit_date"], intraday)
        trades.append(
            {
                "side": side(row),
                "signal": {
                    "kind": "z",
                    "value": row.get("entry_signal_z"),
                    "date": window.signal_dates[entry_date],
                },
                "entry": {
                    "date": entry_date,
                    "spread_fill": row["entry_spread_fill"],
                    "near_price": row["entry_near_fill"],
                    "far_price": row["entry_far_fill"],
                },
                "exit": {
                    "date": exit_date,
                    "spread_fill": row["exit_spread_fill"],
                    "near_price": row["exit_near_fill"],
                    "far_price": row["exit_far_fill"],
                },
                "exit_reason": row["exit_reason"],
                "open_at_end": row["exit_reason"] in FORCED_EXIT_REASONS,
            }
        )
    return trades


def intent_series(window: MeanReversionWindow, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Sizing-pass price rows: spread, eligibility and funding per day."""
    quantity = int(window.spec.execution["quantity"])
    if not window.funding_swaprates:
        return [
            {
                "date": row["date"],
                "benchmark_mark_date": row.get("benchmark_mark_date"),
                "spread": row["spread_value"],
                "volume_eligible": positive_pair_volume(row),
                "funding_rub": None,
                "funding_contracts": 0,
            }
            for row in window.series["rows"]
        ]
    state = holding_flags([row["date"] for row in window.series["rows"]], report["daily"])
    rows: list[dict[str, Any]] = []
    for row in window.series["rows"]:
        day = row["date"]
        daily, _, held = state[day]
        rows.append(
            {
                "date": day,
                "benchmark_mark_date": row.get("benchmark_mark_date"),
                "spread": row["spread_value"],
                "volume_eligible": positive_pair_volume(row),
                "funding_rub": round(daily.get("funding_rub", 0.0), 8)
                if day in window.marks
                else None,
                "funding_contracts": quantity if day in window.funding_swaprates and held else 0,
            }
        )
    return rows


def intent_payload(
    window: MeanReversionWindow,
    report: dict[str, Any],
    funding_provenance: dict[str, Any],
) -> dict[str, Any]:
    """Price-only payload handed to `position_sizing.size_proof`."""
    return {
        "schema_version": PROOF_SCHEMA,
        "universe": PAIR_UNIVERSE,
        "selection": selection_for(window),
        "initial_capital_rub": window.spec.execution["initial_capital_rub"],
        "instrument": instrument_for(window),
        "window": {
            **window_meta(window),
            "passes_capital_screen": window.spec.risk["summary"]["all_scenarios_passed"],
        },
        "contract": {},
        "series": intent_series(window, report),
        "trades": intent_trades(window, report),
        "provenance": funding_provenance,
    }


def proof_payload(
    window: MeanReversionWindow,
    report: dict[str, Any],
    trades: list[dict[str, Any]],
    series_rows: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    source_hashes: dict[str, Any] | None,
) -> dict[str, Any]:
    """Full `execution-proof.v1` payload with chart series, notes and run id."""
    spec, evidence = window.spec, window.evidence
    return pair_payload(
        PairPayloadSpec(
            pair=spec.pair,
            strategy="mean_reversion",
            fill_label=spec.execution["fill"],
            quantity=int(spec.execution["quantity"]),
            slippage=float(spec.execution["slippage_ticks"]),
            params=spec.params,
            terms=spec.terms,
            series_rows=series_rows,
            trades=trades,
            ledger=ledger,
            daily_rows=report["daily"],
            margin_pair=window.margin_pair,
            provenance={
                **window.series["provenance"],
                **(source_hashes or {}),
                **spec.prepared[3],
                **(evidence.provenance if evidence else {}),
            },
            notes=notes(window, trades),
            risk=spec.risk,
            raw_trade_totals=(
                fsum(float(row.get("gross_pnl") or 0.0) for row in report["trades"]),
                fsum(float(row.get("modeled_costs") or 0.0) for row in report["trades"]),
                fsum(float(row.get("net_pnl") or 0.0) for row in report["trades"]),
            ),
        )
    )


def metrics_payload(
    window: MeanReversionWindow,
    trades: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    funding_provenance: dict[str, Any],
) -> dict[str, Any]:
    """Tuning-search shape: trades and ledger only, no chart series or capital curve."""
    return {
        "schema_version": PROOF_SCHEMA,
        "universe": PAIR_UNIVERSE,
        "selection": selection_for(window),
        "initial_capital_rub": window.spec.execution["initial_capital_rub"],
        "instrument": instrument_for(window),
        "window": {
            **window_meta(window),
            "passes_capital_screen": window.spec.risk["summary"]["all_scenarios_passed"],
        },
        "trades": trades,
        "ledger": ledger,
        "provenance": funding_provenance,
    }


def run_mean_reversion(spec: MeanReversionSpec, *, metrics_only: bool = False) -> dict[str, Any]:
    """Run the replay for one prepared window and cut-off, selecting the payload shape."""
    window = _resolve_window(spec)
    prepared = spec.prepared
    funding_provenance, source_hashes = prepared[3], prepared[4]
    report = report_for(window, metrics_only=metrics_only)
    if metrics_only and spec.execution.get("sizing_intents"):
        return intent_payload(window, report, funding_provenance)
    trades = trades_for(window, report)
    series_rows = series_rows_for(window, report)
    ledger = price_ledger(
        series_rows,
        trades,
        window.near_mult,
        window.far_mult,
        int(spec.execution["quantity"]),
    )
    if metrics_only:
        return metrics_payload(window, trades, ledger, funding_provenance)
    return proof_payload(window, report, trades, series_rows, ledger, source_hashes)
