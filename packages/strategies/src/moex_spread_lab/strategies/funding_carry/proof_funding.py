"""Offline funding evidence for execution proof. Raw exchange pages are never patched.

Resolve on the perpetual's own session sequence *before* selecting a far contract or
filtering its volume. A retrospectively estimated payment is not an observed signal.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from moex_spread_lab.core.settings import EvaluationSplit
from moex_spread_lab.core.universe import HISTORY_WINDOWS, PAIRS
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.iss.catalog import fetch_history

from .funding_recheck import format_evidence_notes
from .timeline import (
    DSVD_NOTICE,
    FX_NEW_REGIME,
    FX_PERPETUALS,
    LAUNCH_PERPETUALS,
    MAX_GAP,
    NO_CLEARING,
    POLICY,
    FundingTimeline,
    Mark,
    finite_rate,
    has_settle,
    published_rates,
    read_supplement,
    supplement_path,
)


def _default_offline_only(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("local screener does not download data; run the documented intake first")


def _default_history_source(inputs: Any, pair_id: str, secid: str) -> dict[str, Any]:
    start, end = HISTORY_WINDOWS[pair_id]
    return fetch_history(
        secid,
        start,
        end,
        cache_dir=inputs.history_cache(pair_id),
        delay_seconds=0,
        opener=_default_offline_only,
    )


def _leg_daily_volumes(inputs: Any, pair_id: str, secid: str) -> dict[str, float | None] | None:
    try:
        series = inputs.bars(pair_id, secid, columns=("begin", "volume"))
    except W0Error:
        return None
    vols: dict[str, float | None] = {}
    invalid: set[str] = set()
    for row in series.rows:
        begin = row.get("begin")
        if not isinstance(begin, str) or len(begin) < 10:
            continue
        day = begin[:10]
        value = row.get("volume")
        if value is None or isinstance(value, bool):
            invalid.add(day)
            continue
        number = float(value)
        if not math.isfinite(number) or number < 0:
            invalid.add(day)
            continue
        vols[day] = float(vols.get(day) or 0.0) + number
    for day in invalid:
        vols[day] = None
    return vols


def _default_positive_volume_dates(inputs: Any, pair: Mapping[str, str]) -> set[str]:
    near_vols = _leg_daily_volumes(inputs, pair["pair_id"], pair["near"])
    far_vols = _leg_daily_volumes(inputs, pair["pair_id"], pair["far"])
    if near_vols is None or far_vols is None:
        return set()
    return {
        day
        for day in set(near_vols) & set(far_vols)
        if (nv := near_vols.get(day)) is not None
        and float(nv) > 0
        and (fv := far_vols.get(day)) is not None
        and float(fv) > 0
    }


class ProofFunding:
    """Request-local snapshot, shared across maturities of the same perpetual in the catalog."""

    def __init__(
        self,
        inputs: Any,
        *,
        history_fn: Callable[..., dict[str, Any]] | None = None,
        positive_volume_dates_fn: Callable[..., set[str]] | None = None,
    ) -> None:
        self.inputs = inputs
        self._history_fn = history_fn or _default_history_source
        self._positive_volume_dates_fn = positive_volume_dates_fn or _default_positive_volume_dates
        self._sources: dict[tuple[Path, str, str, str], dict[str, Any]] = {}
        self._timelines: dict[tuple[Path, str, str, str], FundingTimeline] = {}
        self._market_dates: set[str] | None = None

    def source(self, pair_id: str, secid: str) -> dict[str, Any]:
        first, last = HISTORY_WINDOWS[pair_id]
        directory = self.inputs.history_cache(pair_id)
        key = (directory, secid, first, last)
        if key not in self._sources:
            self._sources[key] = self._history_fn(self.inputs, pair_id, secid)
        return self._sources[key]

    def timeline(self, pair: Mapping[str, str]) -> FundingTimeline:
        pid, near = pair["pair_id"], pair["near"]
        start, end = HISTORY_WINDOWS[pid]
        key = (self.inputs.history_cache(pid), near, start, end)
        if key not in self._timelines:
            published = published_rates(near, self.source(pid, near)["rows"])
            candles = self.inputs.bars(pid, near, columns=("begin",)).rows
            days = {
                row["begin"][:10]
                for row in candles
                if isinstance(row.get("begin"), str) and start <= row["begin"][:10] <= end
            }
            verified = {
                d: r
                for d, r in read_supplement(self.inputs.root, near).items()
                if start <= d <= end
            }
            book = FundingTimeline(near, published, days, verified)
            if book.check_spans:
                extra = {
                    d
                    for d in self.market_dates()
                    if d not in published
                    and any(left < d < right for left, right in book.check_spans)
                }
                if extra:
                    book = FundingTimeline(near, published, days, verified, extra)
            self._timelines[key] = book
        return self._timelines[key]

    def market_dates(self) -> set[str]:
        if self._market_dates is None:
            dates: set[str] = set()
            seen: set[tuple[Path, str, str, str]] = set()
            for p in PAIRS:
                pid = p["pair_id"]
                a, b = HISTORY_WINDOWS[pid]
                for secid in (p["near"], p["far"]):
                    directory = self.inputs.history_cache(pid)
                    key = (directory, secid, a, b)
                    if key in seen or not (directory / f"{secid}-{a}-{b}-0.json").is_file():
                        continue
                    seen.add(key)
                    try:
                        source = self._sources.get(key) or self._history_fn(self.inputs, pid, secid)
                        dates.update(str(r["TRADEDATE"]) for r in source["rows"] if has_settle(r))
                    except (ValueError, OSError, TypeError, KeyError):
                        continue
            self._market_dates = dates
        return self._market_dates

    def _verify_perpetual_funding(
        self,
        pair: Mapping[str, str],
        positive_days: set[str],
        funding_files: Sequence[Path],
        reasons: dict[str, str],
    ) -> None:
        try:
            if not funding_files[0].is_file():
                raise W0Error(f"Нет локальных данных SWAPRATE: {funding_files[0].name}.")
            if not positive_days:
                raise W0Error("Нет общих дней с положительным объёмом обеих ног.")
            book = self.timeline(pair)
            needed = book.require(positive_days)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            for name in ("mean_reversion", "funding_only"):
                reasons.setdefault(name, str(exc))
            return
        if "funding_only" in reasons:
            return
        try:
            settles = self.settlement_days(pair)
            needed_settles = {d for d, m in needed.items() if m.kind != "no_clearing"}
            if absent := sorted(needed_settles - settles):
                raise W0Error(
                    f"Нет расчётной цены одной из ног за {absent[0]}. Среднее заменяет только ставку, не цену."
                )
            book.require(settles)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            reasons["funding_only"] = str(exc)

    def statuses(
        self,
        pair: Mapping[str, str],
        positive_days: set[str],
        price_files: Sequence[Path],
        funding_files: Sequence[Path],
    ) -> tuple[dict[str, bool], dict[str, str]]:
        """Different signal inputs; funding permission never grants a missing settlement price."""
        is_perp = pair["family"] == "perpetual_quarterly_same_asset"
        reasons: dict[str, str] = {}
        for name, files in (("mean_reversion", price_files), ("funding_only", funding_files)):
            if name == "funding_only" and not is_perp:
                reasons[name] = "Стратегия фондирования требует вечного фьючерса."
            elif missing := next((p for p in files if not p.is_file()), None):
                reasons[name] = f"Нет локальных данных: {missing.name}."
        if is_perp and len(reasons) < 2:
            self._verify_perpetual_funding(pair, positive_days, funding_files, reasons)
        return {s: s not in reasons for s in ("mean_reversion", "funding_only")}, reasons

    def settlement_days(self, pair: Mapping[str, str]) -> set[str]:
        def _days(secid: str) -> set[str]:
            return {
                str(r["TRADEDATE"])
                for r in self.source(pair["pair_id"], secid)["rows"]
                if has_settle(r)
            }

        return (_days(pair["near"]) & _days(pair["far"])) - self.timeline(pair).no_clearing_dates

    def carry_window(self, pair: Mapping[str, str], share: float) -> FundingWindow:
        book = self.timeline(pair)
        omitted = {
            d
            for d in self._positive_volume_dates_fn(self.inputs, pair)
            if d in book.marks and book.marks[d].kind == "no_clearing"
        }
        return FundingWindow(book, self.settlement_days(pair), share, omitted=omitted)


class FundingWindow:
    """A fixed set of payment sessions; full and prefix cuts use the same evidence."""

    def __init__(
        self,
        book: FundingTimeline,
        days: Iterable[str],
        validation_percent: float = 0,
        *,
        omitted: Iterable[str] = (),
    ) -> None:
        self.book = book
        self.days = tuple(sorted(days))
        self.omitted = tuple(sorted(omitted))
        self.training_end = (
            EvaluationSplit.from_dates(self.days, validation_percent).training[-1]
            if validation_percent
            else None
        )
        self.resolve = cache(self._resolve)
        self.resolve_full = cache(self._resolve_full)

    def _resolve_full(self) -> FundingEvidence:
        """Uncut evidence for the whole window, used by the full proof payload.

        The cut (`through`) only limits the strategy's decisions, not the funding marks
        that are needed to value every journal row of the full period.
        """
        return self._resolve(None)

    def _resolve(self, through: str | None = None) -> FundingEvidence:
        marks = self.book.require(self.days, through=through, training_end=self.training_end)
        if not marks:
            raise W0Error("Нет расчётных дат в выбранном периоде.")
        detail = self.book.provenance(marks)
        omitted = [d for d in self.omitted if through is None or d <= through]
        if omitted:
            detail = {
                **detail,
                "funding_policy": POLICY,
                "funding_no_clearing_dates": omitted,
            }
        return FundingEvidence(
            marks, {d: m.value for d, m in marks.items() if m.value is not None}, detail
        )


@dataclass(slots=True)
class FundingEvidence:
    marks: dict[str, Mark]
    rates: dict[str, float]
    provenance: dict[str, Any]

    def notes(self) -> list[str]:
        return format_evidence_notes(self.provenance)


__all__ = [
    "DSVD_NOTICE",
    "FX_NEW_REGIME",
    "FX_PERPETUALS",
    "LAUNCH_PERPETUALS",
    "MAX_GAP",
    "NO_CLEARING",
    "POLICY",
    "FundingEvidence",
    "FundingTimeline",
    "FundingWindow",
    "Mark",
    "ProofFunding",
    "finite_rate",
    "has_settle",
    "published_rates",
    "read_supplement",
    "supplement_path",
]
