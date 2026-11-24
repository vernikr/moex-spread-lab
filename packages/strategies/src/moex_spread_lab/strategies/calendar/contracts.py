"""Calendar-spread universe constants, contract chains, and input terms/FX loaders."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.account_risk import (
    DEFAULT_STRESS_VM_BUFFER,
    DEFAULT_UNCERTAINTY_BUFFER,
)
from moex_spread_lab.core.settings import INITIAL_CAPITAL, MIN_DAILY_VOLUME
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.universe.assets import (
    ASSETS as ASSETS,
)
from moex_spread_lab.data.universe.assets import (
    CROSS_EXPIRY_PAIRS as CROSS_EXPIRY_PAIRS,
)
from moex_spread_lab.data.universe.assets import (
    FX_SOURCE as FX_SOURCE,
)
from moex_spread_lab.data.universe.assets import (
    MONTH_CODES as MONTH_CODES,
)
from moex_spread_lab.data.universe.assets import (
    QUARTER_CODES as QUARTER_CODES,
)
from moex_spread_lab.data.universe.assets import (
    SYNTHETIC_TRIANGLE_PAIRS as SYNTHETIC_TRIANGLE_PAIRS,
)
from moex_spread_lab.data.universe.assets import (
    contract_chain as contract_chain,
)

from ..base import ZSCORE_CALENDAR_OPTIONS
from ..calendar_eval import FILLS, strategy_runner

HISTORY_FIELDS = (
    "TRADEDATE",
    "SECID",
    "OPEN",
    "LOW",
    "HIGH",
    "CLOSE",
    "SETTLEPRICE",
    "VOLUME",
    "NUMTRADES",
)
EXIT_BUFFER_DAYS = 2
HISTORY_FROM = "2022-06-01"
ZSCORE_OPTIONS = ZSCORE_CALENDAR_OPTIONS
STRATEGIES = ("convergence_to_ruonia", "zscore_mean_reversion")
FILL_LABELS = tuple(label for label, _delay, _fill in FILLS)

MIN_H1_CALENDAR_OBSERVATIONS = 22
MIN_H1_NATIVE_SESSION_VOLUME = MIN_DAILY_VOLUME
MIN_H1_PROXY_LEG_SESSION_VOLUME = MIN_DAILY_VOLUME

_calendar_strategy_runner = strategy_runner


def spread_id(near: str, far: str) -> str:
    return near + far


def _ruonia(benchmark_cache: Path) -> tuple[list[str], dict[str, float]]:
    try:
        rows = json.loads(benchmark_cache.read_text(encoding="utf-8"))["rows"]
        rates = {str(row["date"]): float(row["rate"]) / 100 for row in rows}
    except (OSError, ValueError, KeyError) as exc:
        raise W0Error(f"cannot read RUONIA cache: {exc}") from exc
    return sorted(rates), rates


def _is_pos_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def _terms(contracts_path: Path, assets: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    try:
        instruments = [
            r
            for r in json.loads(contracts_path.read_text(encoding="utf-8"))["instruments"]
            if r.get("expiry") and r["expiry"] < "2100"
        ]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise W0Error(f"cannot read current contract terms: {exc}") from exc
    terms: dict[str, dict[str, Any]] = {}
    for asset in assets:
        live = sorted(
            (
                r
                for r in instruments
                if str(r["secid"]).startswith(asset) and len(str(r["secid"])) == len(asset) + 2
            ),
            key=lambda r: r["expiry"],
        )
        if not live:
            continue
        row = live[0]
        if not all(
            _is_pos_num(row.get(k))
            for k in ("tick_value", "tick_size", "initial_margin", "settlement")
        ):
            continue
        mult = float(row["tick_value"]) / float(row["tick_size"])
        terms[asset] = {
            "mult": mult,
            "tick_value": row["tick_value"],
            "tick_size": row["tick_size"],
            "margin_rate": float(row["initial_margin"]) / (float(row["settlement"]) * mult),
            "reference_contract": row["secid"],
        }
    return terms


def _fx_factors(histories: dict[str, dict[str, dict[str, Any]]]) -> dict[str, dict[str, float]]:
    """RUB value of a point relative to today, from the front quarterly contract of Si or CR."""
    factors: dict[str, dict[str, float]] = {}
    for kind, asset in FX_SOURCE.items():
        chain = [(max(histories[s]), s) for s in contract_chain(asset) if histories.get(s)]
        if not chain:
            continue
        dates = sorted({d for s in contract_chain(asset) for d in histories.get(s, {})})
        scale = 1000.0 if kind == "usd" else 1.0
        fronts: dict[str, float] = {}
        for day in dates:
            live = [
                s
                for expiry, s in sorted(chain)
                if expiry >= day and day in histories[s] and histories[s][day]["SETTLEPRICE"]
            ]
            if live:
                fronts[day] = float(histories[live[0]][day]["SETTLEPRICE"]) / scale
        if fronts:
            latest = fronts[max(fronts)]
            factors[kind] = {day: value / latest for day, value in fronts.items()}
    return factors


def _positive_spread_volume_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row for row in rows if row.get("min_volume") is not None and float(row["min_volume"]) > 0
    ]


def _stress_ok(margin: float, quantity: int = 1, initial_capital: float = INITIAL_CAPITAL) -> bool:
    buffers = (DEFAULT_STRESS_VM_BUFFER + DEFAULT_UNCERTAINTY_BUFFER) * quantity
    return 3 * margin * quantity + buffers <= initial_capital


def _zscore_options(asset: str) -> dict[str, Any]:
    return dict(ZSCORE_OPTIONS["monthly" if ASSETS[asset].get("monthly") else "quarterly"])


# Public intra-package names preserve existing private aliases for callers.
ruonia = _ruonia
terms = _terms
fx_factors = _fx_factors
positive_spread_volume_rows = _positive_spread_volume_rows
stress_ok = _stress_ok
zscore_options = _zscore_options
calendar_strategy_runner = _calendar_strategy_runner
