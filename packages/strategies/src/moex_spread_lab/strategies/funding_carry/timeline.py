"""Offline SWAPRATE timeline interpolation and validation for execution proof."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import W0Error

FX_PERPETUALS = frozenset({"CNYRUBF", "EURRUBF", "USDRUBF"})
FX_NEW_REGIME = "2022-12-05"
MAX_GAP = 3
POLICY = "proof-funding.v1"
DSVD_NOTICE = "https://www.moex.com/n92862"
NO_CLEARING = frozenset({"2025-08-16", "2025-08-17"})
LAUNCH_PERPETUALS = frozenset({"IMOEXF", "GAZPF", "SBERF", "GLDRUBF"})


def finite_rate(value: Any) -> float | None:
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


def published_rates(secid: str, rows: Iterable[Mapping[str, Any]]) -> dict[str, float | None]:
    rates: dict[str, float | None] = {}
    for r in rows:
        if r.get("SECID", secid) != secid:
            raise W0Error(f"SWAPRATE: ожидалась история {secid}.")
        if "SWAPRATE" not in r:
            raise W0Error(
                f"{secid}: в выгрузке отсутствует поле SWAPRATE. Нужна повторная выгрузка ISS."
            )
        day = str(r["TRADEDATE"])
        date.fromisoformat(day)
        rates[day] = finite_rate(r.get("SWAPRATE"))
    return rates


def has_settle(row: Mapping[str, Any]) -> bool:
    v = row.get("SETTLEPRICE")
    try:
        return v is not None and not isinstance(v, bool) and math.isfinite(float(v))
    except (TypeError, ValueError, OverflowError):
        return False


def supplement_path(root: Path | str, secid: str) -> Path:
    return Path(root) / "data/proof-funding-verified" / f"{secid}.json"


def _valid_supplement_record(day: str, record: Any) -> bool:
    if not isinstance(record, Mapping):
        return False
    date.fromisoformat(day)
    hashes = record.get("page_hashes")
    return (
        finite_rate(record.get("rate")) is not None
        and isinstance(hashes, list)
        and bool(hashes)
        and all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes)
        and isinstance(record.get("from"), str)
        and isinstance(record.get("till"), str)
        and (record["from"] <= day <= record["till"])
    )


def read_supplement(root: Path | str, secid: str) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(supplement_path(root, secid).read_text(encoding="utf-8"))
        if data["schema"] != "iss-swaprate-recheck.v1" or data["secid"] != secid:
            raise ValueError
        records = data["records"]
        if not isinstance(records, dict):
            raise ValueError
        for day, record in records.items():
            if not _valid_supplement_record(day, record):
                raise ValueError
        return records
    except FileNotFoundError:
        return {}
    except (TypeError, KeyError, ValueError, AttributeError) as exc:
        raise W0Error(f"Не удалось прочитать повторную сверку SWAPRATE для {secid}.") from exc


@dataclass(frozen=True, slots=True)
class Mark:
    value: float | None
    kind: str
    left: str = ""
    right: str = ""

    def series_fields(self, rates: Mapping[str, Mark]) -> dict[str, Any]:
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


def _format_gap_reason(
    secid: str, d: str, gap: tuple[int, str, str, str | None, str | None, bool, bool] | None
) -> str:
    if not gap:
        return "нет подтверждённой ставки"
    n, first, last, left, right, confirmed, same_regime = gap
    why = f"в истории {secid} с {first} по {last}. Число дат подряд без ставки: {n}"
    if not left or not right:
        why += "; не хватает одной или обеих соседних ставок"
    if not confirmed:
        why += "; для дня без биржевой строки не подтверждён отдельный клиринг"
    if not same_regime:
        why += "; между соседними ставками изменился порядок расчёта"
    if secid in FX_PERPETUALS and d < FX_NEW_REGIME:
        why += ". До 05.12.2022 нужны исторические своп-пункты, а не новый фандинг"
    return why


class FundingTimeline:
    def __init__(
        self,
        secid: str,
        published: Mapping[str, float | None],
        candle_days: Iterable[str],
        verified: Mapping[str, Mapping[str, Any]] | None = None,
        clearing_days: Iterable[str] = (),
    ) -> None:
        self.secid = secid
        self.verified = dict(verified or {})
        self.marks: dict[str, Mark] = {}
        self.gaps: dict[str, tuple[int, str, str, str | None, str | None, bool, bool]] = {}
        self.check_spans: list[tuple[str, str]] = []
        clearing = set(published.keys()) | set(clearing_days) | set(self.verified.keys())
        sessions = self._seed_marks_and_sessions(published, candle_days, clearing)
        self._resolve_session_gaps(sessions, clearing)

    def _seed_marks_and_sessions(
        self,
        published: Mapping[str, float | None],
        candle_days: Iterable[str],
        clearing: set[str],
    ) -> list[str]:
        sessions: list[str] = []
        for d in sorted(set(candle_days) | clearing):
            value = published.get(d)
            verified_value = finite_rate(self.verified[d]["rate"]) if d in self.verified else None
            if self.secid in LAUNCH_PERPETUALS and d in NO_CLEARING:
                if value not in (None, 0) or verified_value not in (None, 0):
                    raise W0Error(
                        f"{self.secid}: ненулевая ставка за {d} противоречит расписанию клиринга MOEX. Нужна сверка источников."
                    )
                self.marks[d] = Mark(None, "no_clearing")
                continue
            if value is not None:
                self.marks[d] = Mark(value, "published")
            elif verified_value is not None:
                self.marks[d] = Mark(verified_value, "rechecked")
            sessions.append(d)
        return sessions

    def _resolve_session_gaps(self, sessions: list[str], clearing: set[str]) -> None:
        i = 0
        while i < len(sessions):
            if sessions[i] in self.marks:
                i += 1
                continue
            start = i
            while i < len(sessions) and sessions[i] not in self.marks:
                i += 1
            self._handle_gap_span(sessions, start, i, clearing)

    def _handle_gap_span(
        self, sessions: list[str], start: int, end_idx: int, clearing: set[str]
    ) -> None:
        days = sessions[start:end_idx]
        left = sessions[start - 1] if start else None
        right = sessions[end_idx] if end_idx < len(sessions) else None
        same_regime = not (
            self.secid in FX_PERPETUALS and left and right and left < FX_NEW_REGIME <= right
        )
        confirmed = all(d in clearing for d in days)
        if (
            left
            and right
            and same_regime
            and len(days) <= MAX_GAP
            and (
                not confirmed
                or (date.fromisoformat(right) - date.fromisoformat(left)).days > len(days) + 1
            )
        ):
            self.check_spans.append((left, right))
        if len(days) <= MAX_GAP and left and right and same_regime and confirmed:
            l_val = self.marks[left].value
            r_val = self.marks[right].value
            assert l_val is not None and r_val is not None
            mark = Mark(l_val / 2 + r_val / 2, "midpoint", left, right)
            self.marks.update(dict.fromkeys(days, mark))
        else:
            self.gaps.update(
                dict.fromkeys(
                    days,
                    (len(days), days[0], days[-1], left, right, confirmed, same_regime),
                )
            )

    @property
    def no_clearing_dates(self) -> set[str]:
        return {d for d in NO_CLEARING if d in self.marks and self.marks[d].kind == "no_clearing"}

    def require(
        self,
        days: Iterable[str],
        *,
        through: str | None = None,
        training_end: str | None = None,
    ) -> dict[str, Mark]:
        """Both anchors must already exist at the evaluation cutoff; never fit from holdout."""
        sorted_days = sorted(days)
        if not sorted_days:
            raise W0Error(f"{self.secid}: нет общих дат для проверки SWAPRATE.")
        end = through or max(sorted_days)
        result: dict[str, Mark] = {}
        for d in sorted_days:
            if d > end:
                continue
            mark = self.marks.get(d)
            if mark is None:
                why = _format_gap_reason(self.secid, d, self.gaps.get(d))
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

    def provenance(self, marks: Mapping[str, Mark]) -> dict[str, Any]:
        estimated = [d for d, m in marks.items() if m.kind == "midpoint"]
        closed = [d for d, m in marks.items() if m.kind == "no_clearing"]
        verified_days = {d for d, m in marks.items() if m.kind == "rechecked"}
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
