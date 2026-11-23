"""Prepare a fixed cohort without changing the search denominator or account state."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean
from typing import Any, Protocol, TypeVar, cast

from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData
from moex_spread_lab.strategies.funding_carry.carry import CarryStrategyError

from . import cohort_models as _cohort_models
from . import cohort_prepare_helpers as _cohort_prepare_helpers
from . import remote_cohort_types as _remote_cohort_types
from . import request_validation as _request_validation
from .axis import Axis
from .landscape import Landscape
from .strategy_catalog import get_strategy_axes

_Obj = dict[str, Any]
_Axes = dict[str, Axis]
_Member = tuple[_Obj, Any]
_CaseT = TypeVar("_CaseT")


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


class _FilterTrainingFn(Protocol):
    def __call__(
        self, members: list[tuple[_Obj, _CaseT]], axes: Mapping[str, Axis], rejected: list[_Obj]
    ) -> list[tuple[_Obj, _CaseT]]: ...


_cm, _ch, _rc = (
    _cohort_models.__dict__,
    _cohort_prepare_helpers.__dict__,
    _remote_cohort_types.__dict__,
)
_Cohort = cast(Callable[..., _CohortLike], _cm["_Cohort"])
_RemoteCohort = cast(Callable[..., _CohortLike], _cm["_RemoteCohort"])
_active_seed = cast(Callable[..., _Obj], _ch["_active_seed"])
_confirmation = cast(Callable[[Any, list[_Obj], Sequence[_Member]], _Obj], _ch["_confirmation"])
_filter_training = cast(_FilterTrainingFn, _ch["_filter_training"])
_intersect_axes = cast(Callable[[_Axes, Mapping[str, Axis], str], None], _ch["_intersect_axes"])
_native_request = cast(Callable[..., _Obj], _ch["_native_request"])
_targets = cast(Callable[[Mapping[str, Any]], list[_Obj]], _ch["_targets"])
_translate = cast(Callable[[Mapping[str, Any], str], _Obj], _ch["_translate"])
_TrainingSlice = cast(Callable[[str, str, int], Any], _rc["_TrainingSlice"])
_RemoteSplit = cast(Callable[[Any], Any], _rc["_RemoteSplit"])
_RemoteCase = cast(Callable[..., _RemoteCaseLike], _rc["_RemoteCase"])
execution_axes = cast(
    Callable[[Mapping[str, Any]], _Axes], _request_validation.__dict__["execution_axes"]
)


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

    land = Landscape(ctx.axes | execution_axes(cast(Mapping[str, Any], inputs.request)), mean)
    return _RemoteCohort(
        active=ctx.active,
        axes=ctx.axes,
        seed=ctx.seed,
        members=members,
        land=land,
        pool=pool,
        cohort_state=cohort_state,
        distinct_strategies=distinct,
        check_cancelled=inputs.check_cancelled,
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

    land = Landscape(ctx.axes | execution_axes(cast(Mapping[str, Any], inputs.request)), average)
    return _Cohort(ctx.active, ctx.axes, ctx.seed, members, land)


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
