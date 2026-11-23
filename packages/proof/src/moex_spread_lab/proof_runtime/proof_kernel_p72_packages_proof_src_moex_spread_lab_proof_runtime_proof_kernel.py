"""Metrics-only training measurement extracted from the strict proof kernel.

The metrics-only fast path keeps a scalar summary instead of a full ledger, so the
annualized comparison is computed here from that summary. The facade passes its own
`_admission` hook, which keeps the documented monkey-patch boundary in one place.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any

from moex_spread_lab.core.evaluation_periods import EvaluationSplit, ResearchSettings

_Obj = dict[str, Any]
_Marks = dict[str, float]
_Admission = Callable[[_Obj, ResearchSettings], "str | None"]
_Summary = tuple[float, float, float, float, float, bool, bool]

SHORTFALL_REASON = "Свободные средства недостаточны для маржи."
UNEXPOSED_REASON = "Открытого спреда не было на обучении."


def _as_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return datetime.combine(date.fromisoformat(value), time.min)


def _annualized(
    initial: float,
    end_wealth: float,
    end_price: float,
    anchor_price: float | None,
    start: str,
    end: str,
) -> _Obj | str:
    """Annualized strategy and LQDT returns, or the reason no finite estimate exists."""
    if (
        anchor_price is None
        or not math.isfinite(anchor_price)
        or anchor_price <= 0
        or not math.isfinite(end_price)
        or end_price <= 0
    ):
        return "Непригодная отметка LQDT в расчётном периоде."
    elapsed_days = (_as_datetime(end) - _as_datetime(start)).total_seconds() / 86400
    if elapsed_days <= 0 or min(initial, end_wealth) <= 0:
        return "Нет положительного капитала или календарного интервала для годовой доходности."
    power = 365 / elapsed_days
    try:
        strategy = 100 * math.expm1(power * math.log(end_wealth / initial))
        benchmark = 100 * math.expm1(power * math.log(end_price / anchor_price))
        excess = strategy - benchmark
        if not all(math.isfinite(v) for v in (strategy, benchmark, excess)):
            raise OverflowError
    except (OverflowError, ValueError):
        return "Годовая оценка не является конечным числом."
    return {
        "strategy_annualized_pct": strategy,
        "lqdt_annualized_pct": benchmark,
        "annualized_excess_pp": 0.0 if abs(excess) < 1e-10 else excess,
        "calendar_days": round(elapsed_days, 4),
    }


def measure_scalar_train(
    proof: _Obj,
    marks: _Marks,
    split: EvaluationSplit,
    settings: ResearchSettings,
    admit: _Admission,
) -> _Obj:
    """Score one metrics-only training replay; missing evidence is never a zero."""
    if reason := admit(proof, settings):
        return {"utility": None, "reason": reason}
    summary: _Summary = proof["_scalar_train_summary"]
    initial, p0, end_price, end_wealth, end_cum_net, shortfall, exposed = summary
    start, end = split.training[0], split.training[-1]
    train_x = end_wealth - initial * end_price / p0
    train_rate = train_x / len(split.training)
    if not all(math.isfinite(v) for v in (train_rate, train_x)):
        return {"utility": None, "reason": "Нет конечной оценки результата."}
    metrics: _Obj = {
        "net_rub": round(end_cum_net, 2),
        "lqdt_on_margin_rub": round(end_cum_net - train_x, 2),
        "train": round(train_rate, 6),
        "validation": None,
        "excess_rub": round(train_x, 2),
        "rub_per_session": round(train_rate, 6),
    }
    reasons = [] if exposed else [UNEXPOSED_REASON]
    annual = _annualized(initial, end_wealth, end_price, marks.get(start), start, end)
    if isinstance(annual, str):
        return {
            **metrics,
            "utility": None,
            "rank_score": None,
            "reason": " ".join([*reasons, annual]),
        }
    if shortfall:
        # A failed cash path keeps the descriptive metrics and blocks only the score.
        return {
            **metrics,
            "rank_score": None,
            "utility": None,
            "reason": " ".join([*reasons, SHORTFALL_REASON]),
        }
    score = annual["annualized_excess_pp"]
    return {
        **metrics,
        **annual,
        "rank_score": score,
        "utility": None if reasons else score,
        **({"reason": " ".join(reasons)} if reasons else {}),
    }
