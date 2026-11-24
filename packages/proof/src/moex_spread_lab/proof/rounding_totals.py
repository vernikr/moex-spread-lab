"""Aggregate raw trade amounts once and disclose display-rounding differences."""

from __future__ import annotations

from collections.abc import Sequence
from math import fsum
from typing import Any


def apply_raw_trade_totals(
    summary: dict[str, Any],
    trades: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    raw_totals: Sequence[float],
) -> None:
    """Update summary only; do not mutate the ledger, trade rows or account curve."""
    gross, costs, net = raw_totals
    for name, value in (("gross", gross), ("costs", costs), ("net", net)):
        key = f"{name}_rub"
        rounded = round(value, 2)
        displayed_sum = round(fsum(float(t[key]) for t in trades), 2)
        delta = round(displayed_sum - rounded, 2)
        summary[key] = rounded
        delta_key = f"displayed_trade_{name}_rounding_delta_rub"
        if delta:
            summary[delta_key] = delta
        else:
            summary.pop(delta_key, None)
    hurdle = fsum(float(t["lqdt_on_margin_rub"]) for t in trades)
    summary["excess_over_lqdt_rub"] = round(net - hurdle, 2)
    cumulative = ledger[-1]["cumulative_net_rub"] if ledger else 0.0
    summary["ledger_reconciliation_rub"] = round(round(net, 2) - cumulative, 2)
