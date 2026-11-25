"""Prepare a fixed cohort without changing the search denominator or account state."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping, Sized
from dataclasses import dataclass
from statistics import fmean
from typing import TYPE_CHECKING, Any, Protocol, cast

from moex_spread_lab.core.contracts import (
    InstrumentContext,
    plugin_parameter_aliases,
    plugin_required_asset_capability,
)
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData
from moex_spread_lab.strategies.funding_carry.carry import CarryStrategyError

from .axis import Axis
from .cohort_models import Cohort as _Cohort
from .cohort_models import RemoteCohort as _RemoteCohort
from .landscape import Landscape
from .request_validation import execution_axes as execution_axes
from .request_validation import identity as _identity
from .strategy_catalog import get_strategy_axes, resolve_strategy_registry

_Obj = dict[str, Any]
_Axes = dict[str, Axis]
_Member = tuple[_Obj, Any]


class _RemoteCaseLike(Protocol):
    request: _Obj
    strategy_axes: _Obj
    execution_axes: _Obj
    axes: _Obj
    seed: _Obj
    split: Any
    _lamp_warning: str | None


class _CohortLike(Protocol):
    active: str
    axes: _Axes
    seed: _Obj
    members: list[Any]
    land: Landscape

    def close(self) -> None: ...
    def plateau(self, point: Any, *, on_progress: Any = None) -> Any: ...
    def report_members(
        self, point: Any, best: Any, settings: Any, check_cancelled: Any, on_progress: Any = None
    ) -> Any: ...


MAX_TARGETS = 1024

if TYPE_CHECKING:
    from moex_spread_lab.strategies import StrategyRegistry


@dataclass
class PreparationInputs:
    service: Any
    request: Any
    settings: Any
    benchmark: Any
    check_cancelled: Any
    calendar: Any
    on_progress: Any
    registry: Any
    window_factory: Any
    can_use_pool: Any
    pool_factory: Any
    unavailable: Any


@dataclass
class _Preparation:
    inputs: PreparationInputs
    targets: list[_Obj]
    checked: bool
    active: str
    strategy_axes: dict[str, _Axes]
    axes: _Axes
    seed: _Obj
    calendar: Any
    early_rejected: dict[int, _Obj]
    valid_specs: list[tuple[int, _Obj, _Obj]]


def _resolve_targets(ctx: _Preparation, proposed: _Obj) -> None:
    inputs = ctx.inputs
    for idx, target in enumerate(ctx.targets):
        inputs.check_cancelled()
        try:
            native = _native_request(
                proposed,
                target,
                default_price_fill=inputs.settings.price_fill,
                registry=inputs.registry,
            )
            ctx.valid_specs.append((idx, target, native))
        except (W0Error, OSError) as exc:
            if not ctx.checked:
                raise
            ctx.early_rejected[idx] = {"target": target, "reason": str(exc)}


def _remote_members(
    ctx: _Preparation, cohort_state: _Obj
) -> tuple[list[tuple[_Obj, _RemoteCaseLike]], list[_Obj]]:
    results = cast(list[tuple[int, str, _Obj]], cohort_state["results"])
    worker_by_idx: dict[int, tuple[str, _Obj]] = {
        idx: (status, info) for idx, status, info in results
    }
    members: list[tuple[_Obj, _RemoteCaseLike]] = []
    rejected: list[_Obj] = []
    for idx, target in enumerate(ctx.targets):
        if idx in ctx.early_rejected:
            rejected.append(ctx.early_rejected[idx])
            continue
        status, info = worker_by_idx[idx]
        if status != "ok":
            rejected.append({"target": target, "reason": info["reason"]})
            continue
        case = _RemoteCase(
            request={"strategy": info["strategy"]},
            strategy_axes=info["strategy_axes"],
            execution_axes=info["execution_axes"],
            axes=info["axes"],
            seed=info["seed"],
            split=_RemoteSplit(
                _TrainingSlice(
                    info["training_first"], info["training_last"], info["training_count"]
                )
            ),
            _lamp_warning=info["lamp_warning"],
        )
        members.append((target, case))
        _intersect_axes(ctx.axes, case.strategy_axes, ctx.active)
    return _filter_training(members, ctx.axes, rejected), rejected


def _remote_cohort(
    ctx: _Preparation,
    pool: Any,
    cohort_state: _Obj,
    members: list[tuple[_Obj, _RemoteCaseLike]],
) -> _CohortLike:
    inputs = ctx.inputs
    distinct: tuple[str, ...] = tuple(
        dict.fromkeys(cast(str, case.request["strategy"]) for _, case in members)
    )
    canary_box: list[int] = [0]
    total_members = len(members)

    def mean(point: _Obj) -> _Obj:
        point_by_strat = {strat: _translate(point, strat) for strat in distinct}
        return cast(
            _Obj,
            pool.eval_point(
                cohort_state, point_by_strat, canary_box, total_members, inputs.check_cancelled
            ),
        )

    land = Landscape(ctx.axes | execution_axes(cast(dict[str, Any], inputs.request)), mean)
    return cast(
        _CohortLike,
        _RemoteCohort(
            active=ctx.active,
            axes=ctx.axes,
            seed=ctx.seed,
            members=cast(Any, members),
            land=land,
            pool=pool,
            cohort_state=cohort_state,
            distinct_strategies=distinct,
            check_cancelled=inputs.check_cancelled,
        ),
    )


def _prepare_remote(ctx: _Preparation) -> _CohortLike | _Obj:
    inputs = ctx.inputs
    pool = inputs.pool_factory(inputs.service)
    items = [(idx, target, native, ctx.checked) for idx, target, native in ctx.valid_specs]
    state = cast(
        _Obj,
        pool.init_cohort(
            inputs.service.root,
            inputs.settings,
            inputs.benchmark,
            items,
            inputs.check_cancelled,
            on_progress=inputs.on_progress,
        ),
    )
    members, rejected = _remote_members(ctx, state)
    if rejected:
        pool.release_cohort(state["cohort_id"], state["owner_of"])
        return _confirmation(inputs.settings, rejected, members)
    return _remote_cohort(ctx, pool, state, members)


def _local_case(ctx: _Preparation, target: _Obj, native: _Obj) -> Any:
    inputs = ctx.inputs
    if ctx.checked and target["universe"] == CALENDAR_UNIVERSE:
        ctx.calendar.check_training_window(
            target["asset"], target["pair"], native.get("quantity", 1), inputs.settings
        )
    # Initialization is not a trial; a clamped seed must not drop a window.
    initial: _Obj = dict(cast(Mapping[str, Any], native["parameters"]))
    strategy = cast(str, native["strategy"])
    for name, axis in ctx.strategy_axes[strategy].items():
        if name in initial:
            initial[name] = min(axis.high, max(axis.low, initial[name]))
    try:
        return inputs.window_factory(
            inputs.service,
            {**native, "parameters": initial},
            inputs.settings,
            inputs.benchmark,
            ctx.calendar,
        )
    except CarryStrategyError:
        if strategy != "funding_only" or initial["lookback"] == 5:
            raise
        return inputs.window_factory(
            inputs.service,
            {**native, "parameters": {"lookback": 5}},
            inputs.settings,
            inputs.benchmark,
            ctx.calendar,
        )


def _admit_local(
    ctx: _Preparation, target: _Obj, native: _Obj, members: list[_Member], rejected: list[_Obj]
) -> _Obj | None:
    inputs = ctx.inputs
    try:
        case = _local_case(ctx, target, native)
        reason = case.admission()
        if reason:
            if not ctx.checked:
                return cast(_Obj, inputs.unavailable(case, reason))
            raise W0Error(reason)
        members.append((target, case))
        _intersect_axes(ctx.axes, case.strategy_axes, ctx.active)
    except (W0Error, OSError) as exc:
        if not ctx.checked:
            raise
        rejected.append({"target": target, "reason": str(exc)})
    return None


def _local_members(ctx: _Preparation) -> tuple[list[_Member], list[_Obj]] | _Obj:
    inputs = ctx.inputs
    members: list[_Member] = []
    rejected: list[_Obj] = []
    spec_by_idx = {idx: native for idx, _, native in ctx.valid_specs}
    total = len(ctx.targets)
    for idx, target in enumerate(ctx.targets):
        inputs.check_cancelled()
        if idx in ctx.early_rejected:
            rejected.append(ctx.early_rejected[idx])
        else:
            unavailable = _admit_local(ctx, target, spec_by_idx[idx], members, rejected)
            if unavailable is not None:
                return unavailable
        if inputs.on_progress is not None:
            inputs.on_progress(idx + 1, total)
    return members, rejected


def _local_cohort(ctx: _Preparation, members: list[_Member]) -> _CohortLike:
    inputs = ctx.inputs

    def average(point: _Obj) -> _Obj:
        scores: list[float] = []
        exposed = False
        for target, case in members:
            inputs.check_cancelled()
            m = cast(_Obj, case.fit.measure(_translate(point, case.request["strategy"])))
            if m.get("rank_score") is None:
                return {
                    "utility": None,
                    "rank_score": None,
                    "reason": f"{target['pair']}: {m.get('reason', 'нет допустимого результата')}",
                }
            scores.append(cast(float, m["rank_score"]))
            exposed |= m["utility"] is not None
        score = fmean(scores)
        return {
            "rank_score": score,
            "by_window_pp": scores,
            "utility": score if exposed else None,
            **({} if exposed else {"reason": "Открытого спреда не было на обучении."}),
        }

    land = Landscape(ctx.axes | execution_axes(cast(dict[str, Any], inputs.request)), average)
    return cast(_CohortLike, _Cohort(ctx.active, ctx.axes, ctx.seed, members, land))


def _prepare_local(ctx: _Preparation) -> _CohortLike | _Obj:
    result = _local_members(ctx)
    if isinstance(result, dict):
        return result
    members, rejected = result
    members = _filter_training(members, ctx.axes, rejected)
    if rejected:
        # Never change the denominator without explicit confirmation.
        return _confirmation(ctx.inputs.settings, rejected, members)
    return _local_cohort(ctx, members)


def prepare_cohort(inputs: PreparationInputs) -> _CohortLike | _Obj:
    request = cast(_Obj, inputs.request)
    active = cast(str, request.get("strategy", "zscore_mean_reversion"))
    strategy_axes = get_strategy_axes(inputs.registry)
    axes = dict(strategy_axes[active])
    seed = _active_seed(request, active, registry=inputs.registry)
    ctx = _Preparation(
        inputs=inputs,
        targets=_targets(request),
        checked=bool(request.get("targets")),
        active=active,
        strategy_axes=strategy_axes,
        axes=axes,
        seed=seed,
        calendar=inputs.calendar
        if inputs.calendar is not None
        else CalendarData(inputs.service.root),
        early_rejected={},
        valid_specs=[],
    )
    proposed: _Obj = {**request, "parameters": {k: seed[k] for k in axes}}
    _resolve_targets(ctx, proposed)
    if inputs.can_use_pool(
        inputs.service, ctx.calendar, ctx.valid_specs, inputs.settings.timeframe
    ):
        return _prepare_remote(ctx)
    return _prepare_local(ctx)


@dataclass
class _TrainingSlice:
    first: str
    last: str
    count: int

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, idx: int) -> str:
        if idx == 0:
            return self.first
        if idx == -1 or idx == self.count - 1:
            return self.last
        raise IndexError(idx)


@dataclass
class _RemoteSplit:
    training: _TrainingSlice


@dataclass
class _RemoteCase:
    request: dict[str, Any]
    strategy_axes: dict[str, Any]
    execution_axes: dict[str, Any]
    axes: dict[str, Any]
    seed: dict[str, Any]
    split: _RemoteSplit
    _lamp_warning: str | None

    def lamp_warning(self):
        return self._lamp_warning


class _SplitLike(Protocol):
    @property
    def training(self) -> Sized: ...


class _CaseLike(Protocol):
    @property
    def split(self) -> _SplitLike: ...


class _SettingsLike(Protocol):
    def metadata(self) -> Mapping[str, Any]: ...


def _parameter_aliases(strategy: str) -> Mapping[str, str]:
    """Incoming-parameter renames declared by the plugin behind ``strategy``."""
    try:
        plugin = resolve_strategy_registry(None).get(strategy)
    except W0Error:
        return {}
    return plugin_parameter_aliases(plugin)


def _translate[ValueT](params: Mapping[str, ValueT], strategy: str) -> dict[str, ValueT]:
    result = dict(params)
    for source, target in _parameter_aliases(strategy).items():
        if source in result:
            result[target] = result.pop(source)
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
    capability = plugin_required_asset_capability(registry.get(strategy))
    if capability and not ASSETS.get(target["asset"], {}).get(capability):
        raise W0Error(
            "Для этого актива не предусмотрена стратегия RUONIA."
            if capability == "convergence"
            else f"Для этого актива не предусмотрена возможность «{capability}»."
        )
    fill = active.get("fill", default_price_fill)
    return {
        **target,
        "strategy": strategy,
        **({"fill": fill} if strategy != "funding_only" else {}),
        **{
            k: active[k]
            for k in ("sizing", *execution_axes(cast(dict[str, Any], active)))
            if k in active
        },
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
            if execution_axes(cast(dict[str, Any], request))
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


# Supported explicit aliases keep the historical hook objects available without
# changing the wildcard surface; private attributes remain the patch points.
active_seed = _active_seed
confirmation = _confirmation
filter_training = _filter_training
intersect_axes = _intersect_axes
native_request = _native_request
targets = _targets
translate = _translate


# Typed internal exports used by other proof modules.
RemoteCase = _RemoteCase
RemoteSplit = _RemoteSplit
TrainingSlice = _TrainingSlice
