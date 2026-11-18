"""One Optuna objective on a fixed, equally weighted cohort of independent proof windows."""

from dataclasses import dataclass
from statistics import fmean

import optuna

from .carry import CarryStrategyError
from .evaluation_periods import ResearchSettings
from .proof import CALENDAR_UNIVERSE, PAIR_UNIVERSE, _benchmark_marks, _run_id
from .proof_search import (
    SEED,
    STRATEGY_AXES,
    Axis,
    Landscape,
    StudyWindow,
    _lamps,
    _tune,
    _validate,
    execution_axes,
)
from .w0 import W0Error

MAX_TARGETS = 1024


def _identity(request):
    u = request.get("universe", CALENDAR_UNIVERSE)
    t = {"universe": u, "pair": request.get("pair")}
    if u == CALENDAR_UNIVERSE:
        t["asset"] = request.get("asset")
    if u not in (PAIR_UNIVERSE, CALENDAR_UNIVERSE) or not all(
        isinstance(v, str) and 0 < len(v) <= 80 for v in t.values()
    ):
        raise W0Error("Некорректный идентификатор окна.")
    return t


def _translate(params, strategy):
    result = dict(params)
    if strategy == "mean_reversion" and "window" in result:
        result["lookback"] = result.pop("window")
    if strategy == "zscore_mean_reversion" and "lookback" in result:
        result["window"] = result.pop("lookback")
    return result


def _native_request(active, target):
    from .calendar_spreads import ASSETS

    strategy = active.get("strategy", "zscore_mean_reversion")
    calendar = target["universe"] == CALENDAR_UNIVERSE
    if strategy in ("mean_reversion", "zscore_mean_reversion"):
        strategy = "zscore_mean_reversion" if calendar else "mean_reversion"
    elif (calendar and strategy == "funding_only") or (
        not calendar and strategy == "convergence_to_ruonia"
    ):
        raise W0Error("Выбранная стратегия неприменима к этому типу пары.")
    if strategy == "convergence_to_ruonia" and not ASSETS.get(target["asset"], {}).get(
        "convergence"
    ):
        raise W0Error("Для этого актива не предусмотрена стратегия RUONIA.")
    fill = active.get("fill", "next_open")
    if strategy == "mean_reversion" and fill != "next_open":
        raise W0Error(
            "У этой пары возврат к среднему исполняется только на следующем открытии."
        )
    return {
        **target,
        "strategy": strategy,
        **({"fill": fill} if calendar else {}),
        **{k: active[k] for k in ("sizing", *execution_axes(active)) if k in active},
        "parameters": _translate(active.get("parameters", {}), strategy),
    }


def _targets(request):
    raw = request.get("targets")
    if "targets" not in request or raw == []:
        return [_identity(request)]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TARGETS:
        raise W0Error(f"Выберите от 1 до {MAX_TARGETS} окон.")
    unique = {}
    for item in raw:
        if not isinstance(item, dict) or item.keys() - {"universe", "asset", "pair"}:
            raise W0Error("Ожидается список идентификаторов окон.")
        t = _identity(item)
        key = t["universe"], t.get("asset", ""), t["pair"]
        if key in unique:
            raise W0Error("Окно указано дважды.")
        unique[key] = t
    return [unique[k] for k in sorted(unique)]


def _active_seed(request, active):
    from .calendar_spreads import ASSETS, ZSCORE_OPTIONS
    from .screener_app import STRATEGIES

    if active in STRATEGIES:
        defaults = STRATEGIES[active]["parameters"]
    elif active == "zscore_mean_reversion":
        freq = (
            "monthly"
            if ASSETS.get(request.get("asset"), {}).get("monthly")
            else "quarterly"
        )
        defaults = {
            "entry_z": 1.5,
            "exit_z": 0.5,
            "window": ZSCORE_OPTIONS[freq]["window"],
        }
    else:
        defaults = {"threshold_pp": 1.0}
    return {
        **defaults,
        **request.get("parameters", {}),
        **({"quantity": request.get("quantity", 1), "slippage_ticks": request.get("slippage_ticks", 1.0)}
           if execution_axes(request) else {}),
    }


@dataclass
class _Cohort:
    active: str
    axes: dict
    seed: dict
    members: list
    land: Landscape

    def plateau(self, point):
        # Never substitute a clamped seed or a smaller, measurable subset for the user's vector.
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
        first = self.members[0][1]
        native_single = (
            len(self.members) == 1 and first.request["strategy"] == self.active
        )
        return _lamps(first.fit if native_single else self.land, point, reason)


def _prepare_cohort(service, request, settings, benchmark, check_cancelled):
    from .calendar_spreads import CalendarData

    targets = _targets(request)
    checked = bool(request.get("targets"))
    active = request.get("strategy", "zscore_mean_reversion")
    axes = dict(STRATEGY_AXES[active])
    seed = _active_seed(request, active)
    proposed = {**request, "parameters": {k: seed[k] for k in axes}}
    members, rejected = [], []
    calendar = CalendarData(service.root)  # Loaded lazily, at most once per request.
    for target in targets:
        check_cancelled()
        try:
            native = _native_request(proposed, target)
            if checked and target["universe"] == CALENDAR_UNIVERSE:
                calendar.check_training_window(
                    target["asset"], target["pair"], native.get("quantity", 1), settings
                )
            # Initialization is not a trial. A seed outside another engine's domain or
            # a long carry warm-up must not make an otherwise compatible window disappear.
            initial = dict(native["parameters"])
            for name, axis in STRATEGY_AXES[native["strategy"]].items():
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
            for name, other in _translate(case.strategy_axes, active).items():
                a = axes[name]
                axes[name] = Axis(
                    max(a.low, other.low), min(a.high, other.high), a.step
                )
        except (W0Error, OSError) as exc:
            if not checked:
                raise
            rejected.append({"target": target, "reason": str(exc)})
    # Some individual histories fit the native domain but not its intersection
    # (e.g. a 5-session calendar combined with a 5-lookback-minimum pair).
    minimum = max(
        (a.low + 2 for k, a in axes.items() if k in ("window", "lookback")), default=2
    )
    compatible = []
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
    members = compatible
    if rejected:
        # Never run against a subset, or change its denominator, without explicit confirmation.
        return {
            "status": "selection_requires_confirmation",
            **settings.metadata(),
            "excluded": rejected,
            "remaining_count": len(members),
            "trial_count": 0,
            "selected_parameters": None,
        }

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


def checked_plateau(service, request, *, check_cancelled=lambda: None):
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
    _validate(request)
    if not request.get("targets"):
        raise W0Error("Для группового светофора нужны отмеченные окна.")
    targets = _targets(request)
    # All live editor values are explicit; never infer the point from a highlighted asset/default.
    active = request.get("strategy")
    if active not in STRATEGY_AXES or set(request.get("parameters", {})) != set(
        STRATEGY_AXES[active]
    ):
        raise W0Error("Для светофора укажите стратегию и все её общие параметры.")
    settings = ResearchSettings.load(service.root)
    benchmark = _benchmark_marks(service.root / "data/benchmark/lqdt-lineage.json")
    group = _prepare_cohort(service, request, settings, benchmark, check_cancelled)
    base = {
        "scope": "checked",
        "member_count": len(targets),
        **settings.metadata(),
        "basis": "training",
    }
    if isinstance(group, dict):
        return {
            **base,
            "status": "unavailable",
            "excluded": group["excluded"],
            "plateau": None,
        }
    return {**base, "status": "measured", "plateau": group.plateau(group.seed)}


def batch_search(service, request, settings, benchmark, check_cancelled):
    group = _prepare_cohort(service, request, settings, benchmark, check_cancelled)
    if isinstance(group, dict):
        return group
    axes, seed, members, land = group.axes, group.seed, group.members, group.land
    first = members[0][1]
    best, trials, attempts = _tune(
        land, seed, axes, min(len(case.split.training) for _, case in members)
    )
    records = [
        {"trial": i, "parameters": {k: p[k] for k in axes}, **land.measure(p)}
        for i, p in enumerate(attempts)
    ]
    point = best or seed
    rows = []
    single = None
    for target, case in members:
        check_cancelled()
        local = _translate(point, case.request["strategy"])
        selection = {
            **case.full["selection"],
            **{k: local[k] for k in case.execution_axes},
            "parameters": {k: local[k] for k in case.strategy_axes},
        }
        # No valid winner: keep the actual requested settings, never apply the preflight seed.
        if not best and any(not a.contains(local[k]) for k, a in case.axes.items()):
            rows.append(
                {
                    "target": target,
                    "reason": "Исходные параметры не входят в общий диапазон.",
                }
            )
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
            rows.append(
                {**row, "run_id": single["run_id"], "evaluation": single["evaluation"]}
            )
        except (W0Error, OSError) as exc:
            rows.append(
                {
                    **row,
                    "evaluation": {
                        **settings.metadata(),
                        **case.split.metadata(),
                        "result": {"reason": str(exc)},
                    },
                }
            )
    rates = [
        r.get("evaluation", {}).get("result", {}).get("annualized_excess_pp")
        for r in rows
    ]
    summary = {
        "member_count": len(rows),
        "unit": "annualized_excess_pp",
        "members": rows,
        **settings.metadata(),
        "result_basis": "validation" if settings.validation_percent else "training",
        "training_mean_pp": land.measure(best)["rank_score"] if best else None,
        "result_mean_pp": fmean(rates)
        if best and all(v is not None for v in rates)
        else None,
    }
    # Same fixed-cohort diagnostic used by the live form, only after the final choice.
    native_single = len(members) == 1 and first.request["strategy"] == group.active
    plateau = group.plateau(point) if best or native_single else None
    result = {
        "schema_version": "proof-search.v3",
        "method": "Optuna TPESampler",
        "library_version": optuna.__version__,
        "seed": SEED,
        "trial_count": trials,
        "candidates": records,
        "selection_basis": "training",
        "objective": "arithmetic_mean_of_per_window_strategy_CAGR_minus_LQDT_CAGR_pp",
        "status": "maximum_found" if best else "no_admissible_candidate",
        "selected_parameters": {k: best[k] for k in axes} if best else None,
        "selection": {k: v for k, v in request.items() if k != "targets"},
        "group": summary,
        "plateau": plateau,
        "reason": None
        if best
        else "Нет допустимого результата сразу для всех выбранных окон.",
    }
    if len(rows) == 1 and single:
        result.update(
            input_run_id=first.full["run_id"],
            result_run_id=single["run_id"],
            evaluation=single["evaluation"],
        )
    return {**result, "run_id": _run_id(result)}


def _unavailable(case, reason):
    result = {
        "schema_version": "proof-search.v3",
        "method": "Optuna TPESampler",
        "status": "unavailable",
        "trial_count": 0,
        "candidates": [],
        "selected_parameters": None,
        "reason": reason,
        "input_run_id": case.full["run_id"],
        "selection": case.request,
        "evaluation": case.report(case.seed)["evaluation"],
        "plateau": _lamps(case.fit, case.seed, reason),
    }
    return {**result, "run_id": _run_id(result)}
