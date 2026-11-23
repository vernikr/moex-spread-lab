"""Prepare a fixed cohort without changing the search denominator or account state."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Any

from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData
from moex_spread_lab.strategies.funding_carry.carry import CarryStrategyError

from .cohort_models import _Cohort, _RemoteCohort
from .cohort_prepare_helpers import (
    _active_seed,
    _confirmation,
    _filter_training,
    _intersect_axes,
    _native_request,
    _targets,
    _translate,
)
from .landscape import Landscape
from .remote_cohort_types import _RemoteCase, _RemoteSplit, _TrainingSlice
from .request_validation import execution_axes
from .strategy_catalog import get_strategy_axes


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
    targets: Any
    checked: bool
    active: str
    strategy_axes: Any
    axes: Any
    seed: Any
    calendar: Any
    early_rejected: Any
    valid_specs: Any


def _resolve_targets(ctx: _Preparation, proposed):
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


def _remote_members(ctx: _Preparation, cohort_state):
    worker_by_idx = {idx: (status, info) for idx, status, info in cohort_state["results"]}
    members, rejected = [], []
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


def _remote_cohort(ctx: _Preparation, pool, cohort_state, members):
    inputs = ctx.inputs
    distinct = tuple(dict.fromkeys(case.request["strategy"] for _, case in members))
    canary_box = [0]
    total_members = len(members)

    def mean(point):
        point_by_strat = {strat: _translate(point, strat) for strat in distinct}
        return pool.eval_point(
            cohort_state, point_by_strat, canary_box, total_members, inputs.check_cancelled
        )

    land = Landscape(ctx.axes | execution_axes(inputs.request), mean)
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


def _prepare_remote(ctx: _Preparation):
    inputs = ctx.inputs
    pool = inputs.pool_factory(inputs.service)
    items = [(idx, target, native, ctx.checked) for idx, target, native in ctx.valid_specs]
    state = pool.init_cohort(
        inputs.service.root,
        inputs.settings,
        inputs.benchmark,
        items,
        inputs.check_cancelled,
        on_progress=inputs.on_progress,
    )
    members, rejected = _remote_members(ctx, state)
    if rejected:
        pool.release_cohort(state["cohort_id"], state["owner_of"])
        return _confirmation(inputs.settings, rejected, members)
    return _remote_cohort(ctx, pool, state, members)


def _local_case(ctx: _Preparation, target, native):
    inputs = ctx.inputs
    if ctx.checked and target["universe"] == CALENDAR_UNIVERSE:
        ctx.calendar.check_training_window(
            target["asset"], target["pair"], native.get("quantity", 1), inputs.settings
        )
    # Initialization is not a trial; a clamped seed must not drop a window.
    initial = dict(native["parameters"])
    for name, axis in ctx.strategy_axes[native["strategy"]].items():
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
        if native["strategy"] != "funding_only" or initial["lookback"] == 5:
            raise
        return inputs.window_factory(
            inputs.service,
            {**native, "parameters": {"lookback": 5}},
            inputs.settings,
            inputs.benchmark,
            ctx.calendar,
        )


def _admit_local(ctx: _Preparation, target, native, members, rejected):
    inputs = ctx.inputs
    try:
        case = _local_case(ctx, target, native)
        reason = case.admission()
        if reason:
            if not ctx.checked:
                return inputs.unavailable(case, reason)
            raise W0Error(reason)
        members.append((target, case))
        _intersect_axes(ctx.axes, case.strategy_axes, ctx.active)
    except (W0Error, OSError) as exc:
        if not ctx.checked:
            raise
        rejected.append({"target": target, "reason": str(exc)})
    return None


def _local_members(ctx: _Preparation):
    inputs = ctx.inputs
    members, rejected = [], []
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


def _local_cohort(ctx: _Preparation, members):
    inputs = ctx.inputs

    def average(point):
        scores, exposed = [], False
        for target, case in members:
            inputs.check_cancelled()
            m = case.fit.measure(_translate(point, case.request["strategy"]))
            if m.get("rank_score") is None:
                return {
                    "utility": None,
                    "rank_score": None,
                    "reason": f"{target['pair']}: {m.get('reason', 'нет допустимого результата')}",
                }
            scores.append(m["rank_score"])
            exposed |= m["utility"] is not None
        score = fmean(scores)
        return {
            "rank_score": score,
            "by_window_pp": scores,
            "utility": score if exposed else None,
            **({} if exposed else {"reason": "Открытого спреда не было на обучении."}),
        }

    land = Landscape(ctx.axes | execution_axes(inputs.request), average)
    return _Cohort(ctx.active, ctx.axes, ctx.seed, members, land)


def _prepare_local(ctx: _Preparation):
    result = _local_members(ctx)
    if isinstance(result, dict):
        return result
    members, rejected = result
    members = _filter_training(members, ctx.axes, rejected)
    if rejected:
        # Never change the denominator without explicit confirmation.
        return _confirmation(ctx.inputs.settings, rejected, members)
    return _local_cohort(ctx, members)


def prepare_cohort(inputs: PreparationInputs):
    request = inputs.request
    active = request.get("strategy", "zscore_mean_reversion")
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
    proposed = {**request, "parameters": {k: seed[k] for k in axes}}
    _resolve_targets(ctx, proposed)
    if inputs.can_use_pool(
        inputs.service, ctx.calendar, ctx.valid_specs, inputs.settings.timeframe
    ):
        return _prepare_remote(ctx)
    return _prepare_local(ctx)
