"""Compact, read-only simulation results for the visible window table.

No optimization, cohort pruning, saved trials or changes to per-window parameters.
Every row is measured by the exact same continuous proof/evaluation as its chart.
"""

from __future__ import annotations

from .calendar_spreads import CalendarData
from .evaluation_periods import ResearchSettings
from .proof import _benchmark_marks
from .proof_batch import _identity
from .proof_search import StudyWindow, _validate
from .w0 import W0Error

MAX_WINDOW_REQUESTS = 128
_DEFAULT_STUDY_WINDOW = StudyWindow


def compact_result(proof):
    summary = proof["summary"]
    return {
        "status": "measured",
        "selection": proof["selection"],
        "run_id": proof["run_id"],
        "trades": summary["trades"],
        "closed_trades": summary["closed_trades"],
        "open_positions": summary["open_positions"],
        "annualized_excess_pp": proof["evaluation"]["result"].get(
            "annualized_excess_pp"
        ),
        "evaluation": proof["evaluation"],
        "window": proof["window"],
        "execution_reference": proof.get("execution_reference", False),
    }


def window_summaries(service, request, *, check_cancelled=lambda: None):
    if not isinstance(request, dict) or request.keys() != {"requests"}:
        raise W0Error("Для таблицы нужен список requests.")
    requests = request["requests"]
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_WINDOW_REQUESTS:
        raise W0Error(f"В таблице допустимо 1–{MAX_WINDOW_REQUESTS} окон за запрос.")
    identities, seen = [], set()
    for item in requests:
        if not isinstance(item, dict) or "targets" in item:
            raise W0Error("Строка таблицы должна описывать одно окно, не группу.")
        target = _identity(item)
        key = tuple(sorted(target.items()))
        if key in seen:
            raise W0Error("Окно таблицы указано дважды.")
        seen.add(key)
        identities.append(target)
    settings = ResearchSettings.load(service.root)
    benchmark = _benchmark_marks(service.root / "data/benchmark/lqdt-lineage.json")
    calendar = service._calendar or CalendarData(service.root)
    if StudyWindow is _DEFAULT_STUDY_WINDOW and type(calendar) is CalendarData:
        from .proof_pool import _estimate_weight

        pool = service.worker_pool()
        est = sum(_estimate_weight(service.root, t, settings.timeframe) for t in identities)
        if pool.should_use(len(requests), est):
            items = list(enumerate(zip(requests, identities)))
            payload = [(idx, item, target) for idx, (item, target) in items]
            rows = pool.window_summaries(
                service.root, settings, benchmark, payload, check_cancelled
            )
            return {**settings.metadata(), "rows": rows}
    rows = []
    for item, target in zip(requests, identities):
        check_cancelled()
        try:
            _validate(item)
            case = StudyWindow(service, item, settings, benchmark, calendar)
            result = compact_result(case.report(case.seed))
        except (W0Error, OSError) as exc:
            result = {
                "status": "unavailable",
                "trades": None,
                "annualized_excess_pp": None,
                "reason": str(exc),
            }
        rows.append({"target": target, **result})
    return {**settings.metadata(), "rows": rows}
