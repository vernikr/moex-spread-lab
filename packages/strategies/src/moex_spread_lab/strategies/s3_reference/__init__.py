"""Audited S3 SBRF historical reference reproduction subpackage."""

from .engine import (
    FILLS,
    REFERENCE_ROUND_TRIP_COST,
    SPREAD_MULTIPLIER,
    account_trades,
    convergence_trades,
    evaluate_variants,
    load_reference,
    read_benchmark,
    run_s3,
    strategy_runner,
    trade_costs,
    zscore_trades,
)

__all__ = [
    "FILLS",
    "REFERENCE_ROUND_TRIP_COST",
    "SPREAD_MULTIPLIER",
    "account_trades",
    "convergence_trades",
    "evaluate_variants",
    "load_reference",
    "read_benchmark",
    "run_s3",
    "strategy_runner",
    "trade_costs",
    "zscore_trades",
]
