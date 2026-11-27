"""Offline pair-screener inputs, candle volume summaries, and funding helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, TypedDict

from moex_spread_lab.core.hashed_json_cache import HashedJSONCache
from moex_spread_lab.core.timeframes import DEFAULT_TIMEFRAME, get_timeframe
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.lake.data_lake import BarSeries, MarketDataStore

from ..funding_carry.carry import fetch_history
from ..funding_carry.proof_funding import ProofFunding
from ..pair_data import (
    HISTORY_WINDOWS,
    PAIRS,
    VolumeMap,
    daily_volumes,
    history_cache_dir,
    history_source,
    leg_volumes,
    positive_leg_days,
)
from .normalization import _normalized_series

__all__ = [
    "DEFAULT_TIMEFRAME",
    "HISTORY_WINDOWS",
    "PAIRS",
    "_CANDLE_SUMMARY_CACHE",
    "BarSeries",
    "MarketDataStore",
    "ProofFunding",
    "ScreenerError",
    "ScreenerInputs",
    "W0Error",
    "_candle_summary",
    "_history_source",
    "_leg_volumes",
    "_make_proof_funding",
    "_offline_only",
    "_pair",
    "_pair_history_span",
    "_positive_leg_volume_dates",
    "_session_volumes",
    "fetch_history",
    "get_timeframe",
    "pair_price_inputs",
]


class CandleSummary(TypedDict):
    """Timestamps and per-session volumes read once per leg series."""

    timestamps: frozenset[str]
    volumes: dict[str, float | None]


class ScreenerError(W0Error):
    """Raised for unavailable data or invalid screener selections."""


@dataclass(frozen=True)
class ScreenerInputs:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    @cached_property
    def bar_store(self) -> MarketDataStore:
        return MarketDataStore(self.root)

    @property
    def benchmark(self) -> Path:
        return self.root / "data/benchmark/lqdt-lineage.json"

    @property
    def contracts(self) -> Path:
        return self.root / "data/w1/current-intake.json"

    @property
    def history(self) -> Path:
        return self.root / "data/analytics/screener-runs.jsonl"

    def candles(self, pair_id: str, secid: str, timeframe: str = DEFAULT_TIMEFRAME) -> Path:
        spec = get_timeframe(timeframe)
        if spec.id == "1h":
            return self.bar_store.select_h1(secid)
        screened = self.root / "data/w1/candles-screen" / f"{secid}_{spec.file_suffix}.json"
        intake = self.root / "data/w1/candles" / f"{secid}_{spec.file_suffix}.json"
        return screened if screened.is_file() else intake

    def bars(
        self,
        pair_id: str,
        secid: str,
        timeframe: str = DEFAULT_TIMEFRAME,
        *,
        columns: tuple[str, ...] | None = None,
    ) -> BarSeries:
        return self.bar_store.read(self.candles(pair_id, secid, timeframe), columns=columns)

    def history_cache(self, pair_id: str) -> Path:
        return history_cache_dir(self.root, pair_id)


def _offline_only(*_args: Any, **_kwargs: Any) -> Any:
    """The screener never downloads; the intake run owns every ISS request."""
    raise OSError("local screener does not download data; run the documented intake first")


def _pair(pair_id: Any) -> dict[str, str]:
    for pair in PAIRS:
        if pair["pair_id"] == pair_id:
            return pair
    raise ScreenerError("пара не входит в допущенный исследовательский реестр")


_CANDLE_SUMMARY_CACHE: HashedJSONCache[tuple[Any, ...], CandleSummary] = HashedJSONCache(2048)


def _candle_summary(series: BarSeries) -> CandleSummary:
    """Cached candle timestamps and per-session volumes for one leg."""
    cached = _CANDLE_SUMMARY_CACHE.get(series.cache_key)
    if cached is not None:
        return cached
    timestamps = {
        str(row["begin"])
        for row in series.rows
        if row.get("begin") and row.get("close") is not None
    }
    result = CandleSummary(timestamps=frozenset(timestamps), volumes=daily_volumes(series.rows))
    _CANDLE_SUMMARY_CACHE.put(series.cache_key, result)
    return result


def _leg_summaries(
    inputs: ScreenerInputs, pair: dict[str, str], timeframe: str = DEFAULT_TIMEFRAME
) -> tuple[CandleSummary, CandleSummary] | None:
    summaries: list[CandleSummary] = []
    for secid in (pair["near"], pair["far"]):
        try:
            summaries.append(
                _candle_summary(
                    inputs.bars(
                        pair["pair_id"], secid, timeframe, columns=("begin", "close", "volume")
                    )
                )
            )
        except W0Error:
            return None
    return summaries[0], summaries[1]


def _session_volumes(
    inputs: ScreenerInputs, pair: dict[str, str], timeframe: str = DEFAULT_TIMEFRAME
) -> dict[str, dict[str, float | None]] | None:
    summaries = _leg_summaries(inputs, pair, timeframe)
    if summaries is None:
        return None
    near_sum, far_sum = summaries
    return {pair["near"]: dict(near_sum["volumes"]), pair["far"]: dict(far_sum["volumes"])}


def _positive_leg_volume_dates(
    inputs: ScreenerInputs, pair: dict[str, str], timeframe: str = DEFAULT_TIMEFRAME
) -> set[str]:
    legs = _session_volumes(inputs, pair, timeframe)
    if legs is None:
        return set()
    return positive_leg_days(_volumes(legs, pair, "near"), _volumes(legs, pair, "far"))


def _volumes(legs: dict[str, dict[str, float | None]], pair: dict[str, str], leg: str) -> VolumeMap:
    return legs[pair[leg]]


def _leg_volumes(
    inputs: ScreenerInputs, pair: dict[str, str], timeframe: str = DEFAULT_TIMEFRAME
) -> dict[str, float] | None:
    legs = _session_volumes(inputs, pair, timeframe)
    if legs is None:
        return None
    near, far = _volumes(legs, pair, "near"), _volumes(legs, pair, "far")
    best = leg_volumes(near, far)
    return None if best is None else {pair["near"]: best["near"], pair["far"]: best["far"]}


def _pair_history_span(
    inputs: ScreenerInputs, pair: dict[str, str], timeframe: str = DEFAULT_TIMEFRAME
) -> tuple[str | None, str | None, int, set[str]]:
    summaries = _leg_summaries(inputs, pair, timeframe)
    if summaries is None:
        return None, None, 0, set()
    near_sum, far_sum = summaries
    common: frozenset[str] = near_sum["timestamps"] & far_sum["timestamps"]
    if not common:
        return None, None, 0, set()
    return min(common)[:10], max(common)[:10], len(common), {ts[:10] for ts in common}


def _history_source(
    inputs: ScreenerInputs,
    pair_id: str,
    secid: str,
    *,
    fetch_fn: Callable[..., dict[str, Any]] = fetch_history,
) -> dict[str, Any]:
    return history_source(inputs.root, pair_id, secid, fetch_fn=fetch_fn)


def _make_proof_funding(
    inputs: ScreenerInputs,
    *,
    fetch_fn: Callable[..., dict[str, Any]] = fetch_history,
) -> ProofFunding:
    def _history(inp: ScreenerInputs, pid: str, secid: str) -> dict[str, Any]:
        return _history_source(inp, pid, secid, fetch_fn=fetch_fn)

    return ProofFunding(
        inputs,
        history_fn=_history,
        positive_volume_dates_fn=_positive_leg_volume_dates,
    )


def pair_price_inputs(
    inputs: ScreenerInputs,
    pair: dict[str, str],
    funding: Any,
    timeframe: str = DEFAULT_TIMEFRAME,
) -> tuple[dict[str, Any], Any]:
    pair_id, near, far = pair["pair_id"], pair["near"], pair["far"]
    spec = get_timeframe(timeframe)
    book = (
        funding.timeline(pair)
        if pair["family"] == "perpetual_quarterly_same_asset" and spec.supports_funding_strategy
        else None
    )
    series = _normalized_series(
        inputs.bars(
            pair_id,
            near,
            timeframe,
            columns=("begin", "open", "close", "volume", "price_proxy_kind"),
        ),
        inputs.bars(
            pair_id,
            far,
            timeframe,
            columns=("begin", "open", "close", "volume", "price_proxy_kind"),
        ),
        inputs.benchmark,
        inputs.contracts,
        near=near,
        far=far,
        prior_benchmark_dates=book.no_clearing_dates if book else (),
        timeframe=timeframe,
    )
    return series, book
