"""Worker-local service and compact window table tasks."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.w0 import W0Error

from . import proof_kernel as _proof_kernel
from .proof_kernel import StudyWindow

if TYPE_CHECKING:
    from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

    from .service import ProofService

# Wildcard surface frozen to the pre-P7.2 revision: star imports keep exposing exactly the
# bindings they exposed before the strict-typing pass, including the ``compact_result``
# kernel hook this module binds.
__all__ = [
    "Any",
    "Path",
    "ResearchSettings",
    "StudyWindow",
    "W0Error",
    "compact_result",
]

_WORKER_SERVICES: dict[str, ProofService] = {}
_KERNEL_EXPORTS = vars(_proof_kernel)
_validate = cast(Callable[[dict[str, Any]], None], _KERNEL_EXPORTS["_validate"])
compact_result = cast(Callable[[dict[str, Any]], dict[str, Any]], _KERNEL_EXPORTS["compact_result"])


class _WorkerServiceLike(Protocol):
    @property
    def calendar_data(self) -> CalendarData: ...


class _StudyWindowLike(Protocol):
    seed: dict[str, Any]

    def report(self, point: dict[str, Any]) -> dict[str, Any]: ...


def _worker_service(root_str: str) -> ProofService:
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
    calendar = cast(_WorkerServiceLike, svc).calendar_data
    rows: list[tuple[int, dict[str, Any]]] = []
    for idx, item, target in items:
        try:
            _validate(item)
            case = cast(_StudyWindowLike, StudyWindow(svc, item, settings, benchmark, calendar))
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


# Explicit aliases preserve identities without changing the historical wildcard surface.
worker_service = _worker_service
worker_window_summaries = _worker_window_summaries


# Typed internal exports used by other proof modules.
