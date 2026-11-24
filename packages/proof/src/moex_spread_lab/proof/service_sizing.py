"""Automatic sizing for request-scoped proof replays."""

from collections.abc import Callable
from typing import Any, cast

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.w0 import W0Error


def prepare_automatic(
    service: Any,
    selection: dict[str, Any],
    calendar: Any,
    settings: ResearchSettings,
    registry: Any,
    strategy: Any,
    facade: Any,
) -> Callable[..., dict[str, Any]]:
    # The caller supplies its module so patched hooks remain dynamic without an import cycle.
    replay = service._prepare_fixed(
        facade._intent_request(selection, registry, settings),
        calendar=calendar,
        settings=settings,
        sizing_intents=True,
    )
    dates, prices = facade._benchmark_marks(service.root / "data/benchmark/lqdt-lineage.json")
    contracts = facade._load_instruments(service.root / "data/w1/current-intake.json")
    marks_cache: dict[str | None, dict[str, float]] = {}
    scan_cols_cache: dict[str | None, Any] = {}

    def sized_replay(
        request: Any,
        through: str | None = None,
        *,
        metrics_only: bool = False,
        scalar_summary: tuple[str, str] | None = None,
        template: bool = False,
    ) -> dict[str, Any]:
        from moex_spread_lab.strategies.base import carry_proof_plugin_ids

        if strategy in carry_proof_plugin_ids(registry) and (
            not isinstance(request, dict)
            or not facade.automatic(request)
            or cast(dict[str, Any], request).get("quantity") is not None
        ):
            raise W0Error("Запрос не соответствует подготовленному автоматическому размеру carry.")
        unit = replay(
            facade._intent_request(request, registry, settings),
            through=through,
            metrics_only=metrics_only,
            **({"template": True} if template else {}),
        )
        unit = facade._calendar_sizing_unit(service.root, request, unit, through, metrics_only)
        marks = marks_cache.get(through)
        if marks is None:
            if not dates or dates[0] > unit["series"][0]["date"]:
                raise W0Error("Нет отметки LQDT на начало истории.")
            marks = {
                r["date"]: facade._mark(
                    dates, prices, r.get("benchmark_mark_date") or r["date"][:10]
                )
                for r in unit["series"]
            }
            marks_cache[through] = marks
        return facade.size_proof(
            unit,
            marks,
            settings,
            contracts,
            metrics_only=metrics_only,
            copy_unit=False,
            _scan_cache=scan_cols_cache if metrics_only else None,
            _scan_key=through,
            scalar_summary=scalar_summary if metrics_only else None,
        )

    return sized_replay
