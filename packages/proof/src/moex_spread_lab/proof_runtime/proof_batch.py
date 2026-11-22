"""One Optuna objective on a fixed, equally weighted cohort of independent proof windows."""

import inspect
from dataclasses import dataclass
from statistics import fmean

import optuna as optuna

from moex_spread_lab.core.contracts import InstrumentContext as InstrumentContext
from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.core.w0 import _run_id as _run_id
from moex_spread_lab.strategies.funding_carry.carry import CarryStrategyError

from .cohort_prepare_helpers import MAX_TARGETS as MAX_TARGETS
from .cohort_prepare_helpers import (
    _active_seed,
    _confirmation,
    _filter_training,
    _intersect_axes,
    _native_request,
    _targets,
    _translate,
)
from .cohort_reporting import CohortReport, build_cohort_result
from .cohort_reporting import _unavailable as _unavailable
from .phase_metrics import measure_phase
from .proof_kernel import (
    TRIALS,
    Landscape,
    StudyWindow,
    _lamps,
    _ordered,
    _tune,
    _validate,
    execution_axes,
    resolve_seed,
)
from .proof_kernel import (
    Axis as Axis,
)
from .proof_pool import _estimate_weight, get_worker_pool
from .remote_cohort_types import _RemoteCase, _RemoteSplit, _TrainingSlice
from .request_validation import _identity as _identity
from .service import _benchmark_marks
from .strategy_catalog import get_strategy_axes
from .strategy_catalog import resolve_strategy_registry as resolve_strategy_registry

_DEFAULT_STUDY_WINDOW = StudyWindow


def _call_with_optional_progress(fn, *args, on_progress=None, **kwargs):
    if on_progress is not None:
        try:
            params = inspect.signature(fn).parameters.values()
        except (TypeError, ValueError):
            params = ()
        if any(p.name == "on_progress" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
            kwargs["on_progress"] = on_progress
    return fn(*args, **kwargs)


@dataclass
class _Cohort:
    active: str
    axes: dict
    seed: dict
    members: list
    land: Landscape

    def close(self) -> None:
        pass

    def _plateau_reason(self, point):
        reason = next(
            (
                f"{k}: общий допустимый диапазон {a.low}–{a.high}."
                for k, a in self.axes.items()
                if not a.contains(point[k])
            ),
            None,
        )
        if not reason:
            reason = next(
                (
                    f"{t['pair']}: {msg}" if len(self.members) > 1 else msg
                    for t, c in self.members
                    if (msg := c.lamp_warning())
                ),
                None,
            )
        return reason

    @measure_phase("plateau")
    def plateau(self, point, *, on_progress=None):
        # Never substitute a clamped seed or a smaller, measurable subset for the user's vector.
        reason = self._plateau_reason(point)
        first = self.members[0][1]
        native_single = len(self.members) == 1 and first.request["strategy"] == self.active
        return _lamps(
            first.fit if native_single else self.land,
            point,
            reason,
            on_progress=on_progress,
        )

    @measure_phase("final_report")
    def report_members(self, point, best, settings, check_cancelled, on_progress=None):
        rows = []
        single = None
        any_exec_ref = any(case.full.get("execution_reference", False) for _, case in self.members)
        total_members = len(self.members)
        for idx, (target, case) in enumerate(self.members, start=1):
            check_cancelled()
            local = _translate(point, case.request["strategy"])
            selection = {
                **case.full["selection"],
                **{k: local[k] for k in case.execution_axes},
                "parameters": {k: local[k] for k in case.strategy_axes},
            }
            if not best and any(not a.contains(local[k]) for k, a in case.axes.items()):
                rows.append(
                    {
                        "target": target,
                        "reason": "Исходные параметры не входят в общий диапазон.",
                    }
                )
                if on_progress is not None:
                    on_progress(idx, total_members)
                continue
            training_rate = case.fit.measure(local).get("rank_score")
            row = {
                "target": target,
                "selection": selection,
                "input_run_id": case.full["run_id"],
                "training_annualized_excess_pp": training_rate,
            }
            try:
                single = case.report(local)
                rows.append({**row, "run_id": single["run_id"], "evaluation": single["evaluation"]})
            except (W0Error, OSError) as exc:
                rows.append(
                    {
                        **row,
                        "evaluation": {
                            **settings.metadata(),
                            **case.split.metadata(),
                            "execution_reference": case.full.get("execution_reference", False),
                            "result": {"reason": str(exc)},
                        },
                    }
                )
            if on_progress is not None:
                on_progress(idx, total_members)
        return rows, any_exec_ref, single


@dataclass
class _RemoteCohort(_Cohort):
    pool: object = None
    cohort_state: dict = None
    distinct_strategies: tuple = ()
    check_cancelled: object = None

    def close(self) -> None:
        if self.pool is not None and self.cohort_state is not None:
            self.pool.release_cohort(self.cohort_state["cohort_id"], self.cohort_state["owner_of"])
            self.cohort_state = None

    @measure_phase("plateau")
    def plateau(self, point, *, on_progress=None):
        reason = self._plateau_reason(point)
        if not reason:
            # Pre-warm center + all valid neighbor points in one parallel batch.
            uncached_points = []
            seen_keys = set()

            def enqueue(candidate):
                key = tuple(candidate[name] for name in self.land.axes)
                if key not in self.land.cache and key not in seen_keys:
                    seen_keys.add(key)
                    point_by_strat = {
                        strat: _translate(candidate, strat) for strat in self.distinct_strategies
                    }
                    uncached_points.append((key, point_by_strat))

            enqueue(point)
            for name, axis in self.land.axes.items():
                value = point[name]
                for offset in (-2, -1, 1, 2):
                    neighbor = round(value + offset * axis.step, 6)
                    other = {**point, name: neighbor}
                    if axis.contains(neighbor) and _ordered(other):
                        enqueue(other)

            if uncached_points:
                batch_payload = [
                    (idx, p_by_strat) for idx, (_, p_by_strat) in enumerate(uncached_points)
                ]
                batch_out = self.pool.eval_plateau_batch(
                    self.cohort_state,
                    batch_payload,
                    self.check_cancelled,
                    on_progress=on_progress,
                )
                for idx, (key, _) in enumerate(uncached_points):
                    self.land.cache[key] = batch_out[idx]
        return _lamps(self.land, point, reason)

    @measure_phase("final_report")
    def report_members(self, point, best, settings, check_cancelled, on_progress=None):
        point_by_strat = {strat: _translate(point, strat) for strat in self.distinct_strategies}
        results = self.pool.report_cohort(
            self.cohort_state,
            point_by_strat,
            not best,
            settings,
            check_cancelled,
            on_progress=on_progress,
        )
        rows = [row for _, row, _, _ in results]
        any_exec_ref = any(exec_ref for _, _, exec_ref, _ in results)
        single = next((s for _, _, _, s in reversed(results) if s is not None), None)
        return rows, any_exec_ref, single


def _can_use_pool(service, calendar, valid_specs, timeframe: str) -> bool:
    from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

    if StudyWindow is not _DEFAULT_STUDY_WINDOW:
        return False
    if type(calendar) is not CalendarData:
        return False
    pool = get_worker_pool(service)
    est = sum(_estimate_weight(service.root, t, timeframe) for _, t, _ in valid_specs)
    return pool.should_use(len(valid_specs), est)


@measure_phase("prepare_cohort")
def _prepare_cohort(
    service,
    request,
    settings,
    benchmark,
    check_cancelled,
    *,
    calendar=None,
    on_progress=None,
    registry=None,
):
    from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

    targets = _targets(request)
    checked = bool(request.get("targets"))
    active = request.get("strategy", "zscore_mean_reversion")
    strategy_axes = get_strategy_axes(registry)
    axes = dict(strategy_axes[active])
    seed = _active_seed(request, active, registry=registry)
    proposed = {**request, "parameters": {k: seed[k] for k in axes}}
    calendar = calendar if calendar is not None else CalendarData(service.root)

    early_rejected = {}
    valid_specs = []
    for idx, target in enumerate(targets):
        check_cancelled()
        try:
            native = _native_request(
                proposed, target, default_price_fill=settings.price_fill, registry=registry
            )
            valid_specs.append((idx, target, native))
        except (W0Error, OSError) as exc:
            if not checked:
                raise
            early_rejected[idx] = {"target": target, "reason": str(exc)}

    if _can_use_pool(service, calendar, valid_specs, settings.timeframe):
        pool = get_worker_pool(service)
        items = [(idx, target, native, checked) for idx, target, native in valid_specs]
        cohort_state = pool.init_cohort(
            service.root,
            settings,
            benchmark,
            items,
            check_cancelled,
            on_progress=on_progress,
        )
        worker_by_idx = {idx: (status, info) for idx, status, info in cohort_state["results"]}
        members, rejected = [], []
        for idx, target in enumerate(targets):
            if idx in early_rejected:
                rejected.append(early_rejected[idx])
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
                        info["training_first"],
                        info["training_last"],
                        info["training_count"],
                    )
                ),
                _lamp_warning=info["lamp_warning"],
            )
            members.append((target, case))
            _intersect_axes(axes, case.strategy_axes, active)
        members = _filter_training(members, axes, rejected)
        if rejected:
            pool.release_cohort(cohort_state["cohort_id"], cohort_state["owner_of"])
            return _confirmation(settings, rejected, members)

        distinct_strategies = tuple(dict.fromkeys(case.request["strategy"] for _, case in members))
        canary_box = [0]
        total_members = len(members)

        def average(point):
            point_by_strat = {strat: _translate(point, strat) for strat in distinct_strategies}
            return pool.eval_point(
                cohort_state, point_by_strat, canary_box, total_members, check_cancelled
            )

        land = Landscape(axes | execution_axes(request), average)
        return _RemoteCohort(
            active=active,
            axes=axes,
            seed=seed,
            members=members,
            land=land,
            pool=pool,
            cohort_state=cohort_state,
            distinct_strategies=distinct_strategies,
            check_cancelled=check_cancelled,
        )

    members, rejected = [], []
    spec_by_idx = {idx: native for idx, _, native in valid_specs}
    total_targets = len(targets)
    for idx, target in enumerate(targets):
        check_cancelled()
        if idx in early_rejected:
            rejected.append(early_rejected[idx])
            if on_progress is not None:
                on_progress(idx + 1, total_targets)
            continue
        native = spec_by_idx[idx]
        try:
            if checked and target["universe"] == CALENDAR_UNIVERSE:
                calendar.check_training_window(
                    target["asset"], target["pair"], native.get("quantity", 1), settings
                )
            # Initialization is not a trial; a clamped seed must not drop a window.
            initial = dict(native["parameters"])
            for name, axis in strategy_axes[native["strategy"]].items():
                if name in initial:
                    initial[name] = min(axis.high, max(axis.low, initial[name]))
            try:
                case = StudyWindow(
                    service,
                    {**native, "parameters": initial},
                    settings,
                    benchmark,
                    calendar,
                )
            except CarryStrategyError:
                if native["strategy"] != "funding_only" or initial["lookback"] == 5:
                    raise
                case = StudyWindow(
                    service,
                    {**native, "parameters": {"lookback": 5}},
                    settings,
                    benchmark,
                    calendar,
                )
            reason = case.admission()
            if reason:
                if not checked:
                    return _unavailable(case, reason)
                raise W0Error(reason)
            members.append((target, case))
            _intersect_axes(axes, case.strategy_axes, active)
        except (W0Error, OSError) as exc:
            if not checked:
                raise
            rejected.append({"target": target, "reason": str(exc)})
        if on_progress is not None:
            on_progress(idx + 1, total_targets)
    members = _filter_training(members, axes, rejected)
    if rejected:
        # Never change the denominator without explicit confirmation.
        return _confirmation(settings, rejected, members)

    def average(point):
        scores, exposed = [], False
        for target, case in members:
            check_cancelled()
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

    land = Landscape(axes | execution_axes(request), average)
    return _Cohort(active, axes, seed, members, land)


def checked_plateau(
    service,
    request,
    *,
    settings_override=None,
    check_cancelled=lambda: None,
    on_progress=None,
    registry=None,
):
    """Read-only probes at one shared point. No focus, Optuna trials, logs or implicit exclusions."""
    request = {
        k: request[k]
        for k in (
            "targets",
            "strategy",
            "sizing",
            "quantity",
            "slippage_ticks",
            "parameters",
            "fill",
        )
        if k in request
    }
    _validate(request, registry=registry)
    if not request.get("targets"):
        raise W0Error("Для группового светофора нужны отмеченные окна.")
    targets = _targets(request)
    strategy_axes = get_strategy_axes(registry)
    active = request.get("strategy")
    if active not in strategy_axes or set(request.get("parameters", {})) != set(
        strategy_axes[active]
    ):
        raise W0Error("Для светофора укажите стратегию и все её общие параметры.")
    settings = settings_override or ResearchSettings.load(service.root)
    benchmark = _benchmark_marks(service.root / "data/benchmark/lqdt-lineage.json")
    if on_progress is not None:
        on_progress(
            {
                "stage": "prepare",
                "done": 0,
                "total": len(targets),
                "percent": 5.0,
                "label": f"Светофор группы: подготовка 0/{len(targets)}…",
            }
        )
    group = _call_with_optional_progress(
        _prepare_cohort,
        service,
        request,
        settings,
        benchmark,
        check_cancelled,
        calendar=service._calendar,
        registry=registry,
        on_progress=(
            None
            if on_progress is None
            else lambda done, total: on_progress(
                {
                    "stage": "prepare",
                    "done": done,
                    "total": total,
                    "percent": round(5.0 + 40.0 * (done / max(total, 1)), 1),
                    "label": f"Светофор группы: подготовка {done}/{total}",
                }
            )
        ),
    )
    base = {
        "scope": "checked",
        "member_count": len(targets),
        **settings.metadata(),
        "execution_reference": (
            request.get("strategy") != "funding_only"
            and request.get("fill", settings.price_fill) == "reference_same_bar"
        ),
        "basis": "training",
    }
    if isinstance(group, dict):
        return {
            **base,
            "status": "unavailable",
            "excluded": group["excluded"],
            "plateau": None,
        }
    try:
        if on_progress is not None:
            on_progress(
                {
                    "stage": "plateau",
                    "done": 0,
                    "total": 1,
                    "percent": 45.0,
                    "label": "Светофор группы: расчёт плато…",
                }
            )
        plateau = group.plateau(
            group.seed,
            on_progress=(
                None
                if on_progress is None
                else lambda done, total: on_progress(
                    {
                        "stage": "plateau",
                        "done": done,
                        "total": total,
                        "percent": round(45.0 + 53.0 * (done / max(total, 1)), 1),
                        "label": f"Светофор группы: проверка плато {done}/{total}",
                    }
                )
            ),
        )
        return {**base, "status": "measured", "plateau": plateau}
    finally:
        group.close()


def batch_search(
    service, request, settings, benchmark, check_cancelled, *, on_progress=None, registry=None
):
    targets = _targets(request)
    if on_progress is not None:
        on_progress(
            {
                "stage": "prepare",
                "done": 0,
                "total": len(targets),
                "percent": 2.0,
                "label": f"Подготовка окон: 0/{len(targets)}…",
            }
        )
    group = _call_with_optional_progress(
        _prepare_cohort,
        service,
        request,
        settings,
        benchmark,
        check_cancelled,
        calendar=service._calendar,
        on_progress=(
            None
            if on_progress is None
            else lambda done, total: on_progress(
                {
                    "stage": "prepare",
                    "done": done,
                    "total": total,
                    "percent": round(2.0 + 10.0 * (done / max(total, 1)), 1),
                    "label": f"Подготовка окон: {done}/{total}",
                }
            )
        ),
    )
    if isinstance(group, dict):
        return group
    try:
        axes, seed, members, land = group.axes, group.seed, group.members, group.land

        best, trials, attempts = _call_with_optional_progress(
            _tune,
            land,
            seed,
            axes,
            min(len(case.split.training) for _, case in members),
            # The base seed is drawn once per run, so seed 0 makes every run
            # different while one run stays internally consistent.
            random_seed=resolve_seed(settings.random_seed),
            seed_label="cohort",
            on_progress=(
                None
                if on_progress is None
                else lambda done, total: on_progress(
                    {
                        "stage": "search",
                        "done": done,
                        "total": total or TRIALS,
                        "percent": round(12.0 + 76.0 * (done / max(total or TRIALS, 1)), 1),
                        "label": f"Optuna: испытание {done}/{total}",
                    }
                )
            ),
        )
        return _cohort_result(
            request,
            settings,
            group,
            best,
            trials,
            attempts,
            check_cancelled,
            on_progress=on_progress,
        )
    finally:
        group.close()


def _cohort_result(
    request, settings, group, best, trials, attempts, check_cancelled, *, on_progress=None
):
    """Compatibility facade for the final cohort report."""
    return build_cohort_result(
        CohortReport(request, settings, group, best, trials, attempts, check_cancelled, on_progress)
    )
