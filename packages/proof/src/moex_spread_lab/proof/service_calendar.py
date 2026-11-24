"""Calendar proof replay and sizing preparation behind the service facade."""

from collections.abc import Callable
from typing import Any, cast

from moex_spread_lab.core.evaluation_periods import ResearchSettings


def calendar_sizing_unit(
    root: Any, request: Any, unit: Any, through: Any, metrics_only: bool, facade: Any
) -> Any:
    if unit.get("schema_version") == "calendar-proof.v1":
        if through is None:
            unit = facade.calendar_proof(root, request, unit)
        else:
            # Partial (training) replays skip the full _calendar_proof
            # enrichment (run_id, notes, summary) to save per-trial work,
            # but the structural markers size_proof/_Terms need must still
            # be present. `universe` routes _Terms to the native branch;
            # `run_id` is the unit identity fed into proof run_id (unused
            # by _measure, only needed when metrics_only is False).
            unit.setdefault("universe", facade.CALENDAR_UNIVERSE)
            if not metrics_only:
                unit.setdefault(
                    "run_id",
                    facade._run_id(
                        {
                            "selection": unit.get("selection", {}),
                            "daily": unit.get("series", []),
                            "source_hashes": {
                                k: v
                                for k, v in cast(
                                    dict[str, Any], unit.get("provenance") or {}
                                ).items()
                                if isinstance(v, str) and "sha256" in k
                            },
                        }
                    ),
                )
    return unit


def prepare_fixed(
    service: Any,
    selection: Any,
    calendar: Any,
    settings: ResearchSettings,
    sizing_intents: Any,
    facade: Any,
) -> Callable[..., dict[str, Any]]:
    """Request-scoped replay of one instrument/strategy with different numeric settings.

    Load the selected inputs once, not the entire registry per trial. Outside this
    request a new session sees refreshed local files. `through` cuts the actual
    simulator input (including carry's tariff reference), not just its output.
    """
    if not isinstance(selection, dict):
        raise facade.W0Error("выбор должен быть объектом")
    selection = cast(dict[str, Any], selection)
    universe = selection.get("universe", facade.CALENDAR_UNIVERSE)
    if universe == facade.PAIR_UNIVERSE:
        from moex_spread_lab.strategies.base import carry_proof_plugin_ids

        from .strategy_catalog import get_strategy_registry

        registry = get_strategy_registry()
        strategy = selection.get("strategy")
        plugin = (
            registry.get(strategy)
            if isinstance(strategy, str) and strategy in carry_proof_plugin_ids(registry)
            else None
        )
        return service._prepare_pair(
            selection, settings, sizing_intents=sizing_intents, carry_plugin=plugin
        )
    if universe != facade.CALENDAR_UNIVERSE:
        raise facade.W0Error("неизвестная вселенная")
    spec = facade.get_timeframe(settings.timeframe)
    if not spec.supports_calendar:
        raise facade.W0Error(
            f"{spec.label}: календарный proof не поддерживается на выбранном интервале."
        )
    calendar = calendar if calendar is not None else service.calendar_data
    cal_cache: dict[str, Any] = {}

    def replay(
        request: Any, through: Any = None, *, metrics_only: bool = False, scalar_summary: Any = None
    ) -> dict[str, Any]:
        effective = {
            **request,
            "timeframe": settings.timeframe,
            **({} if "fill" in request else {"fill": settings.price_fill}),
        }
        payload = calendar.proof(
            effective,
            through=through,
            initial_capital=settings.initial_capital_rub,
            timeframe=settings.timeframe,
            metrics_only=metrics_only,
            sizing_intents=sizing_intents,
            _cache=cal_cache,
        )
        # The search only needs trades, daily marks and admissions from partial replays.
        return (
            payload
            if through is not None
            else facade.calendar_proof(service.root, effective, payload)
        )

    return replay
