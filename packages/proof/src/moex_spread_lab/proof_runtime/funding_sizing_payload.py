"""Price-only funding intent payload consumed by the sizing pass.

Sizing needs settlement spreads and funding only: no costs, capital or chart metadata
are computed here, which keeps the per-trial sizing path cheap.
"""

from __future__ import annotations

from typing import Any

from moex_spread_lab.core.universe import PAIR_UNIVERSE

from .funding_notes import PERPETUAL_OPEN_STATUS
from .payload_tools import PROOF_SCHEMA

__all__ = [
    "funding_instrument",
    "funding_selection",
    "settlement_intent_fill",
    "sizing_intent_payload",
]


def sizing_intent_payload(state: Any, report: dict[str, Any], strategy_name: str) -> dict[str, Any]:
    """Price-only payload for the sizing pass: settlement spreads and funding only."""
    spec = state.spec
    quantity = int(spec.execution["quantity"])
    daily_by_date = {row["date"]: row for row in report["daily"]}
    last_date = report["daily"][-1]["date"]
    trades = []
    for row in report["trades"]:
        entry_date = row["entry_execution_date"]
        exit_date = row["exit_execution_date"] or last_date
        trades.append(
            {
                "side": "short_spread",
                "signal": {
                    "kind": "funding",
                    "value": None,
                    "date": row["entry_decision_date"],
                },
                "entry": settlement_intent_fill(state, daily_by_date[entry_date]),
                "exit": settlement_intent_fill(state, daily_by_date[exit_date]),
                "open_at_end": row["status"] == PERPETUAL_OPEN_STATUS,
            }
        )
    series_rows = [
        {
            "date": row["date"],
            "spread": round(
                row["near_settle"] * state.near_mult - row["far_settle"] * state.far_mult, 8
            ),
            "funding_rub": round(row["daily_funding_included_rub"], 2),
            "funding_contracts": quantity if row["position"] else 0,
        }
        for row in report["daily"]
    ]
    return {
        "schema_version": PROOF_SCHEMA,
        "universe": PAIR_UNIVERSE,
        "selection": funding_selection(state, strategy_name),
        "initial_capital_rub": spec.execution["initial_capital_rub"],
        "instrument": funding_instrument(state),
        "window": {
            **state.window_meta,
            "passes_capital_screen": spec.risk["summary"]["all_scenarios_passed"],
        },
        "contract": {},
        "series": series_rows,
        "trades": trades,
        "provenance": state.evidence.provenance,
    }


def funding_selection(state: Any, strategy_name: str) -> dict[str, Any]:
    spec = state.spec
    return {
        "universe": PAIR_UNIVERSE,
        "pair": spec.pair["pair_id"],
        "strategy": strategy_name,
        "timeframe": "1d",
        "fill": "same_day_settlement",
        "quantity": int(spec.execution["quantity"]),
        "slippage_ticks": float(spec.execution["slippage_ticks"]),
        "parameters": spec.params,
        "initial_capital_rub": spec.execution["initial_capital_rub"],
    }


def funding_instrument(state: Any) -> dict[str, Any]:
    return {
        "near": state.spec.pair["near"],
        "far": state.spec.pair["far"],
        "family": state.spec.pair["family"],
    }


def settlement_intent_fill(state: Any, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": row["date"],
        "spread_fill": round(
            row["near_settle"] * state.near_mult - row["far_settle"] * state.far_mult, 8
        ),
        "near_price": row["near_settle"],
        "far_price": row["far_settle"],
    }
