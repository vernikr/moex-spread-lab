"""Worker-local service and compact window table tasks."""

from pathlib import Path
from typing import Any

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.w0 import W0Error

from .proof_kernel import StudyWindow, _validate, compact_result

_WORKER_SERVICES: dict[str, Any] = {}


def _worker_service(root_str: str):
    svc = _WORKER_SERVICES.get(root_str)
    if svc is None:
        from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

        from .service import ProofService

        root = Path(root_str).resolve()
        svc = ProofService(root, calendar=CalendarData(root))
        _WORKER_SERVICES[root_str] = svc
    return svc


def _worker_window_summaries(
    root_str: str,
    settings: ResearchSettings,
    benchmark: tuple[list[str], dict[str, float]],
    items: list[tuple[int, dict[str, Any], dict[str, Any]]],
) -> list[tuple[int, dict[str, Any]]]:
    svc = _worker_service(root_str)
    calendar = svc._calendar
    rows: list[tuple[int, dict[str, Any]]] = []
    for idx, item, target in items:
        try:
            _validate(item)
            case = StudyWindow(svc, item, settings, benchmark, calendar)
            result = compact_result(case.report(case.seed))
        except (W0Error, OSError) as exc:
            result = {
                "status": "unavailable",
                "trades": None,
                "annualized_excess_pp": None,
                "reason": str(exc),
            }
        rows.append((idx, {"target": target, **result}))
    return rows
