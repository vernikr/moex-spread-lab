"""Request-scoped replay preparation shared by the facade and adapter routes.

Adapter routes need the built-in engine's payload as a market/chart template; letting it
re-enter the adapter branch would recurse into the very plugin being proven.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING, automatic
from moex_spread_lab.core.evaluation_periods import STRICT_SCREENS, ResearchSettings
from moex_spread_lab.core.w0 import W0Error


def check_sizing_request(selection: dict[str, Any]) -> None:
    """Reject sizing modes a request-scoped replay cannot honour."""
    if selection.get("sizing") not in (None, AUTO_SIZING):
        raise W0Error("Неизвестный режим расчёта размера позиции.")
    if automatic(selection) and selection.get("quantity") is not None:
        raise W0Error("При автоматическом размере количество не задаётся вручную.")


def prepare_native(
    service: Any,
    selection: dict[str, Any],
    *,
    calendar: Any = None,
    settings: ResearchSettings = STRICT_SCREENS,
) -> Callable[..., dict[str, Any]]:
    """Prepare a built-in-engine replay, bypassing adapter routing."""
    check_sizing_request(selection)
    if not automatic(selection):
        return service._prepare_fixed(selection, calendar=calendar, settings=settings)
    from .strategy_catalog import get_strategy_registry

    registry = cast(Any, get_strategy_registry())
    return service._prepare_automatic(
        selection, calendar, settings, registry, selection.get("strategy")
    )
