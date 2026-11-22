"""Offline pair-screener catalog builder."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.account_risk import (
    DEFAULT_STRESS_VM_BUFFER,
    DEFAULT_UNCERTAINTY_BUFFER,
)
from moex_spread_lab.core.timeframes import DEFAULT_TIMEFRAME, TimeframeSpec, get_timeframe
from moex_spread_lab.core.universe import (
    HISTORY_WINDOWS,
    INITIAL_CAPITAL,
    MIN_DAILY_VOLUME,
    PAIRS,
    STRATEGIES,
)
from moex_spread_lab.core.w0 import W0Error

from ..funding_carry.carry import fetch_history
from .inputs import ScreenerInputs, _candle_summary, _make_proof_funding


def _load_instruments(inputs: ScreenerInputs) -> dict[str, dict[str, Any]]:
    try:
        instruments = {
            row["secid"]: row
            for row in json.loads(inputs.contracts.read_text(encoding="utf-8"))["instruments"]
            if isinstance(row, dict) and row.get("secid")
        }
    except (OSError, ValueError, KeyError, TypeError):
        instruments = {}
    historical_terms = inputs.root / "data/w1/historical-contracts.json"
    if historical_terms.is_file():
        try:
            rows = json.loads(historical_terms.read_text(encoding="utf-8")).get("instruments", [])
            instruments.update(
                {row["secid"]: row for row in rows if isinstance(row, dict) and row.get("secid")}
            )
        except (OSError, ValueError, TypeError):
            pass
    return instruments


def _pair_bar_stats(
    inputs: ScreenerInputs, pair_id: str, near: str, far: str, timeframe: str
) -> tuple[str | None, str | None, int, set[str], dict[str, float] | None]:
    try:
        near_sum = _candle_summary(
            inputs.bars(pair_id, near, timeframe, columns=("begin", "close", "volume"))
        )
        far_sum = _candle_summary(
            inputs.bars(pair_id, far, timeframe, columns=("begin", "close", "volume"))
        )
    except W0Error:
        return None, None, 0, set(), None
    common_ts = near_sum["timestamps"] & far_sum["timestamps"]
    history_from, history_to, obs = (
        (min(common_ts)[:10], max(common_ts)[:10], len(common_ts)) if common_ts else (None, None, 0)
    )
    near_vols, far_vols = near_sum["volumes"], far_sum["volumes"]
    pos_days = {
        day
        for day in set(near_vols) & set(far_vols)
        if (nv := near_vols.get(day)) is not None
        and float(nv) > 0
        and (fv := far_vols.get(day)) is not None
        and float(fv) > 0
    }
    volumes = (
        {
            near: min(float(near_vols[d]) for d in pos_days),
            far: min(float(far_vols[d]) for d in pos_days),
        }
        if pos_days
        else None
    )
    return history_from, history_to, obs, pos_days, volumes


def _resolve_proof_availability(
    inputs: ScreenerInputs,
    pair: dict[str, str],
    spec: TimeframeSpec,
    funding: Any,
    pos_days: set[str],
    obs: int,
    file_sets: tuple[tuple[Path, ...], tuple[Path, ...]],
) -> tuple[dict[str, bool], dict[str, str]]:
    price_files, funding_files = file_sets
    days = (
        pos_days
        if pair["family"] == "perpetual_quarterly_same_asset" and spec.supports_funding_strategy
        else set()
    )
    if spec.supports_funding_strategy:
        proof_available, reasons = funding.statuses(pair, days, price_files, funding_files)
    else:
        missing_price = next(
            (path for path in price_files if not inputs.bar_store.source_available(path)),
            None,
        )
        proof_available = {
            "mean_reversion": missing_price is None and obs >= 2,
            "funding_only": False,
        }
        reasons = {}
        if missing_price is not None:
            reasons["mean_reversion"] = f"Нет локальных данных: {missing_price.name}."
        elif obs < 2:
            reasons["mean_reversion"] = "Меньше двух общих ценовых наблюдений."
    if not spec.supports_pair_proof:
        proof_available["mean_reversion"] = False
        reasons["mean_reversion"] = (
            f"{spec.label}: ценовой pair-proof не реализован для этого интервала."
        )
    if not spec.supports_funding_strategy:
        proof_available["funding_only"] = False
        reasons["funding_only"] = f"{spec.label}: carry требует дневных расчётных ставок."
    return proof_available, reasons


def _pair_margin_metrics(
    near_terms: dict[str, Any], far_terms: dict[str, Any], initial_capital: float
) -> tuple[float | None, float | None, bool | None]:
    m_near = near_terms.get("initial_margin")
    m_far = far_terms.get("initial_margin")
    margin_pct = (
        round((float(m_near) + float(m_far)) / initial_capital * 100, 2)
        if isinstance(m_near, (int, float)) and isinstance(m_far, (int, float))
        else None
    )
    margin: float | None = None
    if (
        isinstance(m_near, (int, float))
        and not isinstance(m_near, bool)
        and isinstance(m_far, (int, float))
        and not isinstance(m_far, bool)
        and math.isfinite(m_near)
        and math.isfinite(m_far)
        and m_near > 0
        and m_far > 0
    ):
        margin = float(m_near) + float(m_far)
    passes_cap = (
        (3 * margin + DEFAULT_STRESS_VM_BUFFER + DEFAULT_UNCERTAINTY_BUFFER <= initial_capital)
        if margin is not None
        else None
    )
    return margin_pct, margin, passes_cap


def _catalog_pair_entry(
    inputs: ScreenerInputs,
    pair: dict[str, str],
    spec: TimeframeSpec,
    timeframe: str,
    funding: Any,
    instruments: dict[str, dict[str, Any]],
    initial_capital: float,
) -> dict[str, Any]:
    pair_id, near, far = pair["pair_id"], pair["near"], pair["far"]
    h_from, h_to, obs, pos_days, volumes = _pair_bar_stats(inputs, pair_id, near, far, timeframe)
    price_files = (
        inputs.candles(pair_id, near, timeframe),
        inputs.candles(pair_id, far, timeframe),
        inputs.benchmark,
        inputs.contracts,
    )
    start, end = HISTORY_WINDOWS[pair_id]
    funding_files = (
        inputs.history_cache(pair_id) / f"{near}-{start}-{end}-0.json",
        inputs.history_cache(pair_id) / f"{far}-{start}-{end}-0.json",
        inputs.benchmark,
        inputs.contracts,
    )
    tradable = volumes is not None and min(volumes.values()) >= MIN_DAILY_VOLUME
    proof_available, reasons = _resolve_proof_availability(
        inputs, pair, spec, funding, pos_days, obs, (price_files, funding_files)
    )
    available = {
        "mean_reversion": tradable and proof_available["mean_reversion"],
        "funding_only": tradable and proof_available["funding_only"],
    }
    near_terms, far_terms = instruments.get(near, {}), instruments.get(far, {})
    margin_pct, margin, passes_cap = _pair_margin_metrics(near_terms, far_terms, initial_capital)
    status = (
        "excluded_thin_leg"
        if volumes is not None and not tradable
        else "local_inputs_available"
        if any(available.values())
        else "proof_ready_thin_leg"
        if any(proof_available.values())
        else "intake_required"
    )
    return {
        "id": pair_id,
        "timeframe": timeframe,
        "near": near,
        "far": far,
        "family": pair["family"],
        "from": h_from,
        "to": h_to,
        "observations": obs,
        "expiry": far_terms.get("expiry"),
        "current_margin_pct": margin_pct,
        "max_pair_margin_rub": margin,
        "passes_capital_screen": passes_cap,
        "tradable_every_day": tradable if volumes is not None else None,
        "min_daily_volume_rule": MIN_DAILY_VOLUME,
        "min_leg_daily_volume": min(volumes.values()) if volumes else None,
        "strategies": available,
        "proof_strategies": proof_available,
        "data_reasons": reasons,
        "status": status,
    }


def catalog(
    inputs: ScreenerInputs,
    *,
    registry: Sequence[dict[str, str]] = PAIRS,
    funding: Any = None,
    initial_capital: float = INITIAL_CAPITAL,
    timeframe: str = DEFAULT_TIMEFRAME,
    fetch_fn: Callable[..., dict[str, Any]] = fetch_history,
) -> dict[str, Any]:
    spec = get_timeframe(timeframe)
    resolved_funding = funding or _make_proof_funding(inputs, fetch_fn=fetch_fn)
    instruments = _load_instruments(inputs)
    pairs = [
        _catalog_pair_entry(
            inputs, pair, spec, timeframe, resolved_funding, instruments, initial_capital
        )
        for pair in registry
    ]
    return {
        "timeframe": timeframe,
        "pairs": pairs,
        "strategies": STRATEGIES,
        "execution_defaults": {"slippage_ticks": 1.0, "quantity": 1},
        "min_daily_volume": MIN_DAILY_VOLUME,
        "price_warning": "Исторические candle/расчётные значения — аналитические ценовые прокси, не исполнения.",
    }
