"""Canary-slice helpers for the perf-equivalence parity suite (P7.2).

The four default-discovery cases in ``test_perf_equivalence.py`` share
fixed-seed settings, the two screen request payloads and the same-byte-length
cache rewrites used to prove immediate invalidation. They are extracted here so
the canary module stays inside the repository size/complexity policy while every
assertion, trial count and test ID is preserved.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.proof.service import CALENDAR_UNIVERSE, PAIR_UNIVERSE

PAIR_SCREEN_PAIR = "CNYRUBF/CRZ6"
CALENDAR_ASSET = "SR"
CALENDAR_PAIR = "SRH3/SRM3"


def research_settings(*, timeframe: str, initial_capital_rub: float) -> ResearchSettings:
    """Fixed-seed comparison settings; both eval modes must stay comparable."""
    return ResearchSettings(
        timeframe=timeframe,
        validation_percent=20,
        allow_failed_volume=True,
        allow_failed_capital=True,
        initial_capital_rub=initial_capital_rub,
        random_seed=41,
    )


def pair_screen_request(**overrides: Any) -> dict[str, Any]:
    """Pair-screen ``mean_reversion`` request with automatic sizing."""
    request: dict[str, Any] = {
        "universe": PAIR_UNIVERSE,
        "pair": PAIR_SCREEN_PAIR,
        "strategy": "mean_reversion",
        "fill": "next_open",
        "sizing": AUTO_SIZING,
    }
    request.update(overrides)
    return request


def pair_screen_fixed_quantity_request(**overrides: Any) -> dict[str, Any]:
    """Pair-screen request with a manual quantity and no automatic ``sizing`` key."""
    request: dict[str, Any] = {
        "universe": PAIR_UNIVERSE,
        "pair": PAIR_SCREEN_PAIR,
        "strategy": "mean_reversion",
        "fill": "next_open",
        "quantity": 1,
        "slippage_ticks": 1.0,
    }
    request.update(overrides)
    return request


def calendar_spread_request(**overrides: Any) -> dict[str, Any]:
    """Calendar ``zscore_mean_reversion`` request with per-case overrides."""
    request: dict[str, Any] = {
        "universe": CALENDAR_UNIVERSE,
        "asset": CALENDAR_ASSET,
        "pair": CALENDAR_PAIR,
        "strategy": "zscore_mean_reversion",
        "fill": "next_open",
        "sizing": AUTO_SIZING,
    }
    request.update(overrides)
    return request


def convergence_window_requests(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One ``convergence_to_ruonia`` window request per calendar target."""
    return [
        {
            "universe": CALENDAR_UNIVERSE,
            "asset": CALENDAR_ASSET,
            "pair": target["pair"],
            "strategy": "convergence_to_ruonia",
            "parameters": {"threshold_pp": 0.5},
            "fill": "next_open",
            "sizing": AUTO_SIZING,
        }
        for target in targets
    ]


def rewrite_same_size(path: Path, old: str, new: str) -> str:
    """Rewrite ``path`` with a same-byte-length replacement and a fresh mtime."""
    raw = path.read_text(encoding="utf-8")
    mutated = raw.replace(old, new, 1)
    assert len(raw) == len(mutated), f"same-size rewrite changed length: {path}"
    before_stat = path.stat()
    time.sleep(0.02)  # guarantee a different st_mtime_ns on rewrite
    path.write_text(mutated, encoding="utf-8")
    after_stat = path.stat()
    assert before_stat.st_mtime_ns != after_stat.st_mtime_ns, f"mtime did not move: {path}"
    return mutated
