"""Offline pair-screener inputs, candle volume summaries, and funding helpers."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from moex_spread_lab.core.timeframes import DEFAULT_TIMEFRAME, get_timeframe
from moex_spread_lab.core.universe import HISTORY_WINDOWS, PAIRS
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.lake.data_lake import BarSeries, MarketDataStore

from ..funding_carry.carry import fetch_history
from ..funding_carry.proof_funding import ProofFunding
from .screener import _normalized_series


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

    @property
    def screen_history(self) -> Path:
        return self.root / "data/analytics/screener-screens.jsonl"

    @property
    def search_history(self) -> Path:
        return self.root / "data/analytics/screener-searches.jsonl"

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
        folder = (
            "carry-history-cache" if pair_id == "CNYRUBF/CRZ6" else "carry-screen-history-cache"
        )
        return self.root / "data" / folder


def _pair(pair_id: Any) -> dict[str, str]:
    for pair in PAIRS:
        if pair["pair_id"] == pair_id:
            return pair
    raise ScreenerError("пара не входит в допущенный исследовательский реестр")


def _offline_only(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("local screener does not download data; run the documented intake first")


_CANDLE_SUMMARY_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def _parse_row_volume(row: Mapping[str, Any]) -> tuple[str | None, float | None, bool]:
    begin = row.get("begin")
    if not isinstance(begin, str) or len(begin) < 10:
        return None, None, False
    day = begin[:10]
    value = row.get("volume")
    if value is None or isinstance(value, bool):
        return day, None, True
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return day, None, True
    return day, number, False


def _candle_summary(series: BarSeries) -> dict[str, Any]:
    cached = _CANDLE_SUMMARY_CACHE.get(series.cache_key)
    if cached is not None:
        return cached
    timestamps: set[str] = set()
    volumes: dict[str, float | None] = {}
    invalid_days: set[str] = set()
    for row in series.rows:
        begin = row.get("begin")
        if begin and row.get("close") is not None:
            timestamps.add(str(begin))
        day, num, invalid = _parse_row_volume(row)
        if day is None:
            continue
        if invalid:
            invalid_days.add(day)
        elif num is not None:
            volumes[day] = float(volumes.get(day) or 0.0) + num
    for day in invalid_days:
        volumes[day] = None
    result: dict[str, Any] = {"timestamps": frozenset(timestamps), "volumes": volumes}
    if len(_CANDLE_SUMMARY_CACHE) > 2048:
        _CANDLE_SUMMARY_CACHE.clear()
    _CANDLE_SUMMARY_CACHE[series.cache_key] = result
    return result


def _session_volumes(
    inputs: ScreenerInputs, pair: dict[str, str], timeframe: str = DEFAULT_TIMEFRAME
) -> dict[str, dict[str, float | None]] | None:
    legs: dict[str, dict[str, float | None]] = {}
    for secid in (pair["near"], pair["far"]):
        try:
            summary = _candle_summary(
                inputs.bars(pair["pair_id"], secid, timeframe, columns=("begin", "close", "volume"))
            )
        except W0Error:
            return None
        legs[secid] = dict(summary["volumes"])
    return legs


def _is_positive_day(
    legs: dict[str, dict[str, float | None]], pair: dict[str, str], day: str
) -> bool:
    for secid in (pair["near"], pair["far"]):
        val = legs[secid].get(day)
        if val is None or float(val) <= 0:
            return False
    return True


def _leg_volumes(
    inputs: ScreenerInputs, pair: dict[str, str], timeframe: str = DEFAULT_TIMEFRAME
) -> dict[str, float] | None:
    legs = _session_volumes(inputs, pair, timeframe)
    if legs is None:
        return None
    days = set(legs[pair["near"]]) & set(legs[pair["far"]])
    eligible = [day for day in days if _is_positive_day(legs, pair, day)]
    if not eligible:
        return None
    return {
        secid: min(float(legs[secid][day]) for day in eligible)  # type: ignore[arg-type]
        for secid in legs
    }


def _positive_leg_volume_dates(
    inputs: ScreenerInputs, pair: dict[str, str], timeframe: str = DEFAULT_TIMEFRAME
) -> set[str]:
    legs = _session_volumes(inputs, pair, timeframe)
    if legs is None:
        return set()
    return {
        day
        for day in set(legs[pair["near"]]) & set(legs[pair["far"]])
        if _is_positive_day(legs, pair, day)
    }


def _pair_history_span(
    inputs: ScreenerInputs, pair: dict[str, str], timeframe: str = DEFAULT_TIMEFRAME
) -> tuple[str | None, str | None, int, set[str]]:
    legs: list[frozenset[str]] = []
    for secid in (pair["near"], pair["far"]):
        try:
            summary = _candle_summary(
                inputs.bars(pair["pair_id"], secid, timeframe, columns=("begin", "close", "volume"))
            )
        except W0Error:
            return None, None, 0, set()
        legs.append(summary["timestamps"])
    common = legs[0] & legs[1]
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
    start, end = HISTORY_WINDOWS[pair_id]
    return fetch_fn(
        secid,
        start,
        end,
        cache_dir=inputs.history_cache(pair_id),
        delay_seconds=0,
        opener=_offline_only,
    )


def _make_proof_funding(
    inputs: ScreenerInputs,
    *,
    fetch_fn: Callable[..., dict[str, Any]] = fetch_history,
) -> ProofFunding:
    return ProofFunding(
        inputs,
        history_fn=lambda inp, pid, secid: _history_source(inp, pid, secid, fetch_fn=fetch_fn),
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
