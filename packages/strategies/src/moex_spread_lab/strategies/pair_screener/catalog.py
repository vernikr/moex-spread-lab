"""Offline pair-screener catalog builder."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

from moex_spread_lab.accounting.account_risk import (
    DEFAULT_STRESS_VM_BUFFER,
    DEFAULT_UNCERTAINTY_BUFFER,
)
from moex_spread_lab.core.settings import INITIAL_CAPITAL, MIN_DAILY_VOLUME
from moex_spread_lab.core.timeframes import DEFAULT_TIMEFRAME, TimeframeSpec, get_timeframe
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data import universe as _catalog
from moex_spread_lab.strategies.registry import builtin_pair_strategies

from ..funding_carry.study import fetch_history
from ..pair_data import leg_volumes, positive_leg_days
from .inputs import ScreenerInputs, _candle_summary, _make_proof_funding

# Typed aliases of the core research registry: same objects, precise static types.
HISTORY_WINDOWS: dict[str, tuple[str, str]] = _catalog.HISTORY_WINDOWS
PAIRS: tuple[dict[str, str], ...] = _catalog.PAIRS


def _builtin_strategies_payload() -> dict[str, dict[str, Any]]:
    return {
        pid: {
            "label": plugin.label_ru,
            "parameters": {spec.name: spec.default for spec in plugin.parameter_schema()},
        }
        for pid, plugin in builtin_pair_strategies().items()
    }


STRATEGIES: dict[str, dict[str, Any]] = _builtin_strategies_payload()

InstrumentRow = dict[str, Any]
Instruments = dict[str, InstrumentRow]
PairEntry = dict[str, Any]
FileSet = tuple[Path, ...]


class FundingStatuses(Protocol):
    """The narrow funding-evidence surface the catalog needs."""

    def statuses(
        self,
        pair: Mapping[str, str],
        positive_days: set[str],
        price_files: Sequence[Path],
        funding_files: Sequence[Path],
    ) -> tuple[dict[str, bool], dict[str, str]]: ...


def _instrument_rows(payload: object) -> Iterable[InstrumentRow]:
    """Yield well-formed instrument rows from an already parsed intake document."""
    if not isinstance(payload, dict):
        return
    source = cast(dict[str, Any], payload)
    rows = source.get("instruments")
    if not isinstance(rows, list):
        return
    for row in cast(list[Any], rows):
        if isinstance(row, dict) and cast(dict[str, Any], row).get("secid"):
            yield cast(dict[str, Any], row)


def _read_instruments(path: Path) -> Instruments:
    """Read one intake document; an unreadable, malformed or rowless file yields no rows."""
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {cast(str, row["secid"]): row for row in _instrument_rows(payload)}


def _load_instruments(inputs: ScreenerInputs) -> Instruments:
    """Current intake first, then historical terms layered on top (unknown keys kept)."""
    instruments = _read_instruments(inputs.contracts)
    instruments.update(_read_instruments(inputs.root / "data/w1/historical-contracts.json"))
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
    pos_days = positive_leg_days(near_sum["volumes"], far_sum["volumes"])
    best = leg_volumes(near_sum["volumes"], far_sum["volumes"])
    return (
        history_from,
        history_to,
        obs,
        pos_days,
        (None if best is None else {near: best["near"], far: best["far"]}),
    )


def _resolve_proof_availability(
    inputs: ScreenerInputs,
    pair: dict[str, str],
    spec: TimeframeSpec,
    funding: FundingStatuses,
    pos_days: set[str],
    obs: int,
    file_sets: tuple[FileSet, FileSet],
) -> tuple[dict[str, bool], dict[str, str]]:
    price_files, funding_files = file_sets
    days: set[str] = (
        pos_days
        if pair["family"] == "perpetual_quarterly_same_asset" and spec.supports_funding_strategy
        else set()
    )
    proof_available: dict[str, bool]
    reasons: dict[str, str]
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
    near_terms: InstrumentRow, far_terms: InstrumentRow, initial_capital: float
) -> tuple[float | None, float | None, bool | None]:
    m_near: object = near_terms.get("initial_margin")
    m_far: object = far_terms.get("initial_margin")
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
    funding: FundingStatuses,
    instruments: Instruments,
    initial_capital: float,
) -> PairEntry:
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
    absent_terms: InstrumentRow = {}
    margin_pct, margin, passes_cap = _pair_margin_metrics(
        instruments.get(near, absent_terms), instruments.get(far, absent_terms), initial_capital
    )
    status = (
        "excluded_thin_leg"
        if volumes is not None and not tradable
        else "local_inputs_available"
        if any(available.values())
        else "proof_ready_thin_leg"
        if any(proof_available.values())
        else "intake_required"
    )
    entry: PairEntry = {
        "id": pair_id,
        "timeframe": timeframe,
        "near": near,
        "far": far,
        "family": pair["family"],
        "from": h_from,
        "to": h_to,
        "observations": obs,
        "expiry": instruments.get(far, absent_terms).get("expiry"),
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
    return entry


def catalog(
    inputs: ScreenerInputs,
    *,
    registry: Sequence[dict[str, str]] = PAIRS,
    funding: FundingStatuses | None = None,
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
