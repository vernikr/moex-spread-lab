"""Causal, maximum-affordable sizing of proof signal intents on one finite account."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from moex_spread_lab.core._fastcore_loader import load_fastcore

from .sizing_fast import _STD_COST_KEYS, _size_proof_fast_c
from .sizing_replay import finalize_sized_proof, replay_sizing_series
from .sizing_terms import (
    AUTO_SIZING,
    SIZING_POLICY,
    _money,
    _positive,
    _Terms,
    automatic,
    maximum_quantity,
)


def size_proof(
    unit: dict[str, Any],
    marks: dict[str, float],
    settings: Any,
    contracts: dict[str, Any],
    *,
    metrics_only: bool = False,
    copy_unit: bool = True,
    _scan_cache: dict[Any, Any] | None = None,
    _scan_key: Any = None,
    scalar_summary: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Replay independent signal intents; all account observations are prefix-causal."""
    initial = _positive(settings.initial_capital_rub, "начальный капитал")
    proof = (
        {**unit, "selection": dict(unit["selection"])}
        if metrics_only
        else deepcopy(unit)
        if copy_unit
        else unit
    )
    terms = _Terms(proof, contracts, settings)
    strategy_name = proof["selection"]["strategy"]
    if metrics_only:
        fc = load_fastcore()
        if fc is not None:
            result = _size_proof_fast_c(
                fc,
                proof,
                terms,
                marks,
                (settings, initial, strategy_name),
                scan_cache_pair=(_scan_cache, _scan_key),
                scalar_summary=scalar_summary,
            )
            if result is not None:
                return result

    state = replay_sizing_series(
        proof,
        marks,
        settings,
        terms,
        initial,
        strategy_name,
        metrics_only=metrics_only,
    )
    return finalize_sized_proof(
        unit,
        proof,
        state,
        marks,
        settings,
        terms,
        metrics_only=metrics_only,
    )


__all__ = [
    "AUTO_SIZING",
    "SIZING_POLICY",
    "_STD_COST_KEYS",
    "_Terms",
    "_money",
    "_positive",
    "_size_proof_fast_c",
    "automatic",
    "maximum_quantity",
    "size_proof",
]
