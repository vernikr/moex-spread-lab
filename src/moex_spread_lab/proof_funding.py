"""Offline funding evidence for execution proof. Raw exchange pages are never patched.

Resolve on the perpetual's own session sequence *before* selecting a far contract or
filtering its volume. A retrospectively estimated payment is not an observed signal.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path

from .evaluation_periods import EvaluationSplit
from .w0 import W0Error

FX_PERPETUALS = frozenset({"CNYRUBF", "EURRUBF", "USDRUBF"})
FX_NEW_REGIME = "2022-12-05"  # MOEX n52418: do not interpolate across different cash-flow definitions.
MAX_GAP = 3
POLICY = "proof-funding.v1"
DSVD_NOTICE = "https://www.moex.com/n92862"
# Verified launch weekend (MOEX n92862), not a weekday() heuristic. A working
# Saturday or a holiday is not automatically a zero payment. No extra Monday rate.
NO_CLEARING = frozenset({"2025-08-16", "2025-08-17"})
LAUNCH_PERPETUALS = frozenset({"IMOEXF", "GAZPF", "SBERF", "GLDRUBF"})


def finite_rate(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise W0Error("SWAPRATE: некорректное значение ставки в источнике.")
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise W0Error("SWAPRATE: ставка в источнике не является числом.") from exc
    if not math.isfinite(number):
        raise W0Error("SWAPRATE: ставка в источнике должна быть конечной.")
    return number


def published_rates(secid, rows):
    rates = {}
    for r in rows:
        if r.get("SECID", secid) != secid:
            raise W0Error(f"SWAPRATE: ожидалась история {secid}.")
        if "SWAPRATE" not in r:
            raise W0Error(
                f"{secid}: в выгрузке отсутствует поле SWAPRATE. Нужна повторная выгрузка ISS."
            )
        day = r["TRADEDATE"]
        date.fromisoformat(day)
        rates[day] = finite_rate(r.get("SWAPRATE"))
    return rates


def has_settle(row):
    v = row.get("SETTLEPRICE")
    try:
        return v is not None and not isinstance(v, bool) and math.isfinite(float(v))
    except (TypeError, ValueError, OverflowError):
        return False


def supplement_path(root, secid):
    return Path(root) / "data/proof-funding-verified" / f"{secid}.json"


def read_supplement(root, secid):
    try:
        data = json.loads(supplement_path(root, secid).read_text(encoding="utf-8"))
        if data["schema"] != "iss-swaprate-recheck.v1" or data["secid"] != secid:
            raise ValueError
        records = data["records"]
        for day, record in records.items():
            date.fromisoformat(day)
            hashes = record["page_hashes"]
            if (
                finite_rate(record["rate"]) is None
                or not isinstance(hashes, list)
                or not hashes
                or not all(
                    isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h)
                    for h in hashes
                )
                or not (record["from"] <= day <= record["till"])
            ):
                raise ValueError
        return records
    except FileNotFoundError:
        return {}
    except (TypeError, KeyError, ValueError, AttributeError) as exc:
        raise W0Error(
            f"Не удалось прочитать повторную сверку SWAPRATE для {secid}."
        ) from exc


@dataclass(frozen=True, slots=True)
class Mark:
    # None means no separate clearing, not a published numerical zero.
    value: float | None
    kind: str
    left: str = ""
    right: str = ""

    def series_fields(self, rates):
        if self.kind == "published":
            return {}
        if self.kind == "midpoint":
            return {
                "funding_source": "Estimated SWAPRATE: mean of two published endpoints",
                "funding_rate_kind": self.kind,
                "funding_left_date": self.left,
                "funding_right_date": self.right,
                "funding_left_rate": rates[self.left].value,
                "funding_right_rate": rates[self.right].value,
            }
        if self.kind == "no_clearing":
            return {"funding_source": DSVD_NOTICE, "funding_rate_kind": self.kind}
        return {
            "funding_source": "MOEX ISS SWAPRATE; verified recheck",
            "funding_rate_kind": self.kind,
        }


class FundingTimeline:
    def __init__(self, secid, published, candle_days, verified=None, clearing_days=()):
        self.secid = secid
        self.verified = verified or {}
        self.marks = {}
        self.gaps = {}
        sessions = []
        clearing = published.keys() | set(clearing_days) | self.verified.keys()
        self.check_spans = []
        for d in sorted(set(candle_days) | clearing):
            value = published.get(d)
            verified_value = (
                finite_rate(self.verified[d]["rate"]) if d in self.verified else None
            )
            if secid in LAUNCH_PERPETUALS and d in NO_CLEARING:
                if value not in (None, 0) or verified_value not in (None, 0):
                    raise W0Error(
                        f"{secid}: ненулевая ставка за {d} противоречит расписанию клиринга MOEX. Нужна сверка источников."
                    )
                self.marks[d] = Mark(None, "no_clearing")
                continue  # not an endpoint and not a missing funding session
            if value is not None:
                self.marks[d] = Mark(value, "published")
            elif verified_value is not None:
                self.marks[d] = Mark(verified_value, "rechecked")
            sessions.append(d)
        i = 0
        while i < len(sessions):
            if sessions[i] in self.marks:
                i += 1
                continue
            start = i
            while i < len(sessions) and sessions[i] not in self.marks:
                i += 1
            days = sessions[start:i]
            left = sessions[start - 1] if start else None
            right = sessions[i] if i < len(sessions) else None
            same_regime = not (
                secid in FX_PERPETUALS
                and left
                and right
                and left < FX_NEW_REGIME <= right
            )
            confirmed = all(d in clearing for d in days)
            if (
                left
                and right
                and same_regime
                and len(days) <= MAX_GAP
                and (
                    not confirmed
                    or (date.fromisoformat(right) - date.fromisoformat(left)).days
                    > len(days) + 1
                )
            ):
                self.check_spans.append((left, right))
            if len(days) <= MAX_GAP and left and right and same_regime and confirmed:
                value = self.marks[left].value / 2 + self.marks[right].value / 2
                mark = Mark(value, "midpoint", left, right)
                self.marks.update(dict.fromkeys(days, mark))
            else:
                self.gaps.update(
                    dict.fromkeys(
                        days,
                        (
                            len(days),
                            days[0],
                            days[-1],
                            left,
                            right,
                            confirmed,
                            same_regime,
                        ),
                    )
                )

    @property
    def no_clearing_dates(self):
        return {
            d
            for d in NO_CLEARING
            if d in self.marks and self.marks[d].kind == "no_clearing"
        }

    def require(self, days, *, through=None, training_end=None):
        """Both anchors must already exist at the evaluation cutoff; never fit from holdout."""
        if not days:
            raise W0Error(f"{self.secid}: нет общих дат для проверки SWAPRATE.")
        end = through or max(days)
        result = {}
        for d in sorted(days):
            if d > end:
                continue
            mark = self.marks.get(d)
            if mark is None:
                gap = self.gaps.get(d)
                why = "нет подтверждённой ставки"
                if gap:
                    n, first, last, left, right, confirmed, same_regime = gap
                    why = f"в истории {self.secid} с {first} по {last}. Число дат подряд без ставки: {n}"
                    if not left or not right:
                        why += "; не хватает одной или обеих соседних ставок"
                    if not confirmed:
                        why += "; для дня без биржевой строки не подтверждён отдельный клиринг"
                    if not same_regime:
                        why += "; между соседними ставками изменился порядок расчёта"
                    if self.secid in FX_PERPETUALS and d < FX_NEW_REGIME:
                        why += ". До 05.12.2022 нужны исторические своп-пункты, а не новый фандинг"
                raise W0Error(
                    f"SWAPRATE за {d}: {why}. Среднее допустимо для 1–3 сессий с двумя известными границами."
                )
            cutoff = training_end if training_end and d <= training_end else end
            if mark.kind == "midpoint" and mark.right > min(cutoff, end):
                raise W0Error(
                    f"{self.secid}: для SWAPRATE за {d} нужна ставка {mark.right}, за границей периода {min(cutoff, end)}."
                )
            result[d] = mark
        return result

    def provenance(self, marks):
        estimated = [d for d, m in marks.items() if m.kind == "midpoint"]
        closed = [d for d, m in marks.items() if m.kind == "no_clearing"]
        verified_days = {d for d, m in marks.items() if m.kind == "rechecked"}
        # Include rechecked observations used only as interpolation anchors, too.
        for d in estimated:
            for edge in (marks[d].left, marks[d].right):
                if self.marks[edge].kind == "rechecked":
                    verified_days.add(edge)
        verified = {d: self.verified[d] for d in sorted(verified_days)}
        if not (estimated or closed or verified):
            return {}
        return {
            "funding_policy": POLICY,
            "funding_estimated_dates": estimated,
            "funding_estimates": {
                d: {
                    "left": marks[d].left,
                    "left_rate": self.marks[marks[d].left].value,
                    "right": marks[d].right,
                    "right_rate": self.marks[marks[d].right].value,
                    "rate": marks[d].value,
                }
                for d in estimated
            },
            "funding_no_clearing_dates": closed,
            "funding_recheck_pages": verified,
        }


class ProofFunding:
    """Request-local snapshot, shared across maturities of the same perpetual in the catalog."""

    def __init__(self, inputs):
        self.inputs = inputs
        self._sources = {}
        self._timelines = {}
        self._market_dates = None

    def source(self, pair_id, secid):
        from .screener_app import HISTORY_WINDOWS, _history_source

        first, last = HISTORY_WINDOWS[pair_id]
        directory = self.inputs.history_cache(pair_id)
        key = directory, secid, first, last
        if key not in self._sources:
            self._sources[key] = _history_source(self.inputs, pair_id, secid)
        return self._sources[key]

    def timeline(self, pair):
        from .screener_app import HISTORY_WINDOWS

        pid, near = pair["pair_id"], pair["near"]
        start, end = HISTORY_WINDOWS[pid]
        key = self.inputs.history_cache(pid), near, start, end
        if key not in self._timelines:
            published = published_rates(near, self.source(pid, near)["rows"])
            candles = json.loads(self.inputs.candles(pid, near).read_text())["rows"]
            days = {r["begin"][:10] for r in candles if start <= r["begin"][:10] <= end}
            verified = {
                d: r
                for d, r in read_supplement(self.inputs.root, near).items()
                if start <= d <= end
            }
            book = FundingTimeline(near, published, days, verified)
            if book.check_spans:
                # An internal candidate may have absent EOD *rows*, even dates
                # missing from BOTH near files. Consult other local FORTS settles
                # to count/confirm these sessions. Never depend on which sparse
                # far leg happens to be checked. Do not extend the near lifetime.
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

    def market_dates(self):
        from .screener_app import HISTORY_WINDOWS, PAIRS, _history_source

        if self._market_dates is None:
            dates, seen = set(), set()
            for p in PAIRS:
                pid = p["pair_id"]
                a, b = HISTORY_WINDOWS[pid]
                for secid in (p["near"], p["far"]):
                    directory = self.inputs.history_cache(pid)
                    key = directory, secid, a, b
                    if (
                        key in seen
                        or not (directory / f"{secid}-{a}-{b}-0.json").is_file()
                    ):
                        continue
                    seen.add(key)
                    try:
                        source = self._sources.get(key) or _history_source(
                            self.inputs, pid, secid
                        )
                        dates.update(
                            r["TRADEDATE"] for r in source["rows"] if has_settle(r)
                        )
                    except (ValueError, OSError, TypeError, KeyError):
                        continue  # not evidence of a market clearing
            self._market_dates = dates
        return self._market_dates

    def statuses(self, pair, positive_days, price_files, funding_files):
        """Different signal inputs; funding permission never grants a missing settlement price."""
        is_perp = pair["family"] == "perpetual_quarterly_same_asset"
        reasons = {}
        for name, files in (
            ("mean_reversion", price_files),
            ("funding_only", funding_files),
        ):
            if name == "funding_only" and not is_perp:
                reasons[name] = "Стратегия фондирования требует вечного фьючерса."
            elif missing := next((p for p in files if not p.is_file()), None):
                reasons[name] = f"Нет локальных данных: {missing.name}."
        if is_perp and len(reasons) < 2:
            try:
                if not funding_files[0].is_file():
                    raise W0Error(
                        f"Нет локальных данных SWAPRATE: {funding_files[0].name}."
                    )
                if not positive_days:
                    raise W0Error("Нет общих дней с положительным объёмом обеих ног.")
                book = self.timeline(pair)
                needed = book.require(positive_days)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                for name in ("mean_reversion", "funding_only"):
                    reasons.setdefault(name, str(exc))
            else:
                if "funding_only" not in reasons:
                    try:
                        settles = self.settlement_days(pair)
                        needed_settles = {
                            d for d, m in needed.items() if m.kind != "no_clearing"
                        }
                        if absent := sorted(needed_settles - settles):
                            raise W0Error(
                                f"Нет расчётной цены одной из ног за {absent[0]}. Среднее заменяет только ставку, не цену."
                            )
                        book.require(settles)
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        reasons["funding_only"] = str(exc)
        return {
            s: s not in reasons for s in ("mean_reversion", "funding_only")
        }, reasons

    def settlement_days(self, pair):
        def days(secid):
            return {
                r["TRADEDATE"]
                for r in self.source(pair["pair_id"], secid)["rows"]
                if has_settle(r)
            }

        return (days(pair["near"]) & days(pair["far"])) - self.timeline(
            pair
        ).no_clearing_dates

    def carry_window(self, pair, share):
        from .screener_app import _positive_leg_volume_dates

        book = self.timeline(pair)
        omitted = {
            d
            for d in _positive_leg_volume_dates(self.inputs, pair)
            if d in book.marks and book.marks[d].kind == "no_clearing"
        }
        return FundingWindow(book, self.settlement_days(pair), share, omitted=omitted)


class FundingWindow:
    """A fixed set of payment sessions; full and prefix cuts use the same evidence."""

    def __init__(self, book, days, validation_percent=0, *, omitted=()):
        self.book = book
        self.days = tuple(sorted(days))
        self.omitted = tuple(
            sorted(omitted)
        )  # documented sessions without a settlement candle in carry
        self.training_end = (
            EvaluationSplit.from_dates(self.days, validation_percent).training[-1]
            if validation_percent
            else None
        )
        # Per instance, not a global cache holding finished research requests alive.
        self.resolve = cache(self._resolve)

    def _resolve(self, through=None):
        marks = self.book.require(
            self.days, through=through, training_end=self.training_end
        )
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
    marks: dict
    rates: dict  # only actual or estimated rates; no clearing => no row in the monetary rate map
    provenance: dict

    def notes(self):
        notes = []
        if days := self.provenance.get("funding_estimated_dates"):
            notes.append(
                f"Расчётных ставок: {len(days)}. Для серии из 1–3 пропущенных расчётных сессий на каждый день взято одно среднее двух соседних ставок. Это оценка платежа, не публикация MOEX; для сигнала она доступна только после публикации правой границы."
            )
        if days := self.provenance.get("funding_no_clearing_dates"):
            notes.append(
                "Без отдельного клиринга: "
                + ", ".join(days)
                + ". Дополнительная сессия входит в следующий торговый день. Отдельного платежа нет; расчётная цена не подставляется. MOEX: "
                + DSVD_NOTICE
            )
        if self.provenance.get("funding_recheck_pages"):
            notes.append(
                "Для пропусков использована также отдельная повторная выгрузка MOEX ISS; исходный кэш сохранён."
            )
        return notes
