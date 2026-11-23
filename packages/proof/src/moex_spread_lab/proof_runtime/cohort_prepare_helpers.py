"""Request mapping and common admission helpers for cohort preparation."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sized
from typing import TYPE_CHECKING, Any, Protocol, cast

from moex_spread_lab.core.contracts import InstrumentContext
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from . import request_validation as _request_validation
from .axis import Axis
from .strategy_catalog import get_strategy_axes, resolve_strategy_registry

if TYPE_CHECKING:
    from moex_spread_lab.strategies import StrategyRegistry

MAX_TARGETS = 1024

# Wildcard surface frozen to the pre-P7.2 revision: star imports keep exposing exactly the
# bindings they exposed before the strict-typing pass. The private helpers stay module
# attributes under their historical names for ``proof_batch`` / ``cohort_prepare`` /
# ``cohort_models``, which bind and patch them here; ``_facade_hooks`` keeps those
# references live without widening this list.
__all__ = [
    "CALENDAR_UNIVERSE",
    "MAX_TARGETS",
    "Axis",
    "InstrumentContext",
    "W0Error",
    "annotations",
    "execution_axes",
    "get_strategy_axes",
    "resolve_strategy_registry",
]

_Obj = dict[str, Any]


class _SplitLike(Protocol):
    @property
    def training(self) -> Sized: ...


class _CaseLike(Protocol):
    @property
    def split(self) -> _SplitLike: ...


class _SettingsLike(Protocol):
    def metadata(self) -> Mapping[str, Any]: ...


# ``request_validation`` keeps its untyped definitions (it is owned elsewhere), so rebind
# the two hooks to the original callables with explicit signatures; this module keeps both
# names and their runtime identity under whole-package strict typing.
_identity = cast(Callable[[Mapping[str, Any]], _Obj], _request_validation.__dict__["_identity"])
execution_axes = cast(
    Callable[[Mapping[str, Any]], Mapping[str, Axis]],
    _request_validation.__dict__["execution_axes"],
)


def _translate[ValueT](params: Mapping[str, ValueT], strategy: str) -> dict[str, ValueT]:
    result = dict(params)
    if strategy == "mean_reversion" and "window" in result:
        result["lookback"] = result.pop("window")
    if strategy == "zscore_mean_reversion" and "lookback" in result:
        result["window"] = result.pop("lookback")
    return result


def _native_request(
    active: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    default_price_fill: str = "reference_same_bar",
    registry: StrategyRegistry | None = None,
) -> _Obj:
    from moex_spread_lab.strategies.calendar.calendar_spreads import ASSETS

    registry = resolve_strategy_registry(registry)
    strategy_axes = get_strategy_axes(registry)
    strategy = active.get("strategy", "zscore_mean_reversion")
    if strategy not in strategy_axes:
        raise W0Error("Неизвестная стратегия подбора")
    plugin = registry.get(strategy)
    adapters = [
        p
        for p in registry
        if p.strategy_id in strategy_axes
        and p.family_id == plugin.family_id
        and target["universe"] in p.supported_universes
    ]
    if not adapters:
        raise W0Error("Выбранная стратегия неприменима к этому типу пары.")
    strategy = adapters[0].strategy_id
    if strategy == "convergence_to_ruonia" and not ASSETS.get(target["asset"], {}).get(
        "convergence"
    ):
        raise W0Error("Для этого актива не предусмотрена стратегия RUONIA.")
    fill = active.get("fill", default_price_fill)
    return {
        **target,
        "strategy": strategy,
        **({"fill": fill} if strategy != "funding_only" else {}),
        **{k: active[k] for k in ("sizing", *execution_axes(active)) if k in active},
        "parameters": _translate(active.get("parameters", {}), strategy),
    }


def _targets(request: Mapping[str, Any]) -> list[_Obj]:
    raw = request.get("targets")
    if "targets" not in request or raw == []:
        return [_identity(request)]
    # Typed up front only to avoid ``list[Unknown]`` narrowing; the guard below still
    # decides, at runtime, whether ``raw`` is a list of an admissible size.
    items = cast(list[object], raw)
    if not isinstance(raw, list) or not 1 <= len(items) <= MAX_TARGETS:
        raise W0Error(f"Выберите от 1 до {MAX_TARGETS} окон.")
    unique: dict[tuple[Any, Any, Any], _Obj] = {}
    for item in items:
        window = cast(_Obj, item)
        if not isinstance(item, dict) or window.keys() - {"universe", "asset", "pair"}:
            raise W0Error("Ожидается список идентификаторов окон.")
        t = _identity(window)
        key = t["universe"], t.get("asset", ""), t["pair"]
        if key in unique:
            raise W0Error("Окно указано дважды.")
        unique[key] = t
    return [unique[k] for k in sorted(unique)]


def _active_seed(
    request: Mapping[str, Any], active: str, *, registry: StrategyRegistry | None = None
) -> _Obj:
    from moex_spread_lab.strategies.calendar.calendar_spreads import ASSETS

    asset: Any = request.get("asset")
    context = InstrumentContext(
        universe=request.get("universe", CALENDAR_UNIVERSE),
        pair=request.get("pair", ""),
        asset=asset,
        monthly=bool(ASSETS.get(asset, {}).get("monthly")),
    )
    plugin = resolve_strategy_registry(registry).get(active)
    defaults = {
        spec.name: spec.default for spec in plugin.parameter_schema(context) if spec.tunable
    }
    return {
        **defaults,
        **request.get("parameters", {}),
        **(
            {
                "quantity": request.get("quantity", 1),
                "slippage_ticks": request.get("slippage_ticks", 1.0),
            }
            if execution_axes(request)
            else {}
        ),
    }


def _intersect_axes(
    axes: MutableMapping[str, Axis], strategy_axes: Mapping[str, Axis], active: str
) -> None:
    for name, other in _translate(strategy_axes, active).items():
        a = axes[name]
        axes[name] = Axis(max(a.low, other.low), min(a.high, other.high), a.step)


def _filter_training[CaseT: _CaseLike](
    members: Iterable[tuple[_Obj, CaseT]], axes: Mapping[str, Axis], rejected: list[_Obj]
) -> list[tuple[_Obj, CaseT]]:
    # Every window must fit the intersection, not merely its individual domain.
    minimum = max((a.low + 2 for k, a in axes.items() if k in ("window", "lookback")), default=2)
    compatible: list[tuple[_Obj, CaseT]] = []
    for target, case in members:
        if len(case.split.training) >= minimum:
            compatible.append((target, case))
        else:
            rejected.append(
                {
                    "target": target,
                    "reason": f"Для общего окна сигнала нужно не менее {minimum} наблюдений на обучении.",
                }
            )
    return compatible


def _confirmation(settings: _SettingsLike, rejected: list[_Obj], members: Sized) -> _Obj:
    return {
        "status": "selection_requires_confirmation",
        **settings.metadata(),
        "excluded": rejected,
        "remaining_count": len(members),
        "trial_count": 0,
        "selected_parameters": None,
    }


# These helpers are never called here, but ``proof_batch`` re-exports them and
# ``cohort_prepare`` / ``cohort_models`` resolve them through this module at call time.
# Referencing them keeps that surface live without widening ``__all__``.
_facade_hooks = (
    _active_seed,
    _confirmation,
    _filter_training,
    _intersect_axes,
    _native_request,
    _targets,
    _translate,
)
