"""Bounded Optuna search and local, one-field-at-a-time diagnostics for execution proof.

The sampler proposes strategy parameters, never execution assumptions. The same proof
simulators evaluate proposals, sensitivity probes and the displayed portfolio.
"""

from __future__ import annotations

import hashlib
import math as _math
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date as date
from datetime import datetime as datetime
from datetime import time as time
from functools import cached_property
from typing import Any, Protocol, cast

import optuna

from moex_spread_lab.accounting.position_sizing import automatic
from moex_spread_lab.core.settings import (
    EvaluationSplit,
    ResearchSettings,
)
from moex_spread_lab.core.timeframes import get_timeframe as get_timeframe
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.core.w0 import run_id as _run_id

from .axis import Axis
from .axis import ordered as _ordered
from .evaluation import account_checks as account_checks
from .evaluation import annualized_returns as _annualized_returns
from .evaluation import excess_curve as _excess_curve
from .evaluation import missing_exposure as _missing_exposure
from .landscape import AMBER as AMBER
from .landscape import GREEN as GREEN
from .landscape import Landscape
from .landscape import lamps as _lamps
from .phase_metrics import measure_phase as measure_phase
from .request_validation import EXECUTION_AXES as EXECUTION_AXES
from .request_validation import compact_result as compact_result
from .request_validation import execution_axes as execution_axes
from .request_validation import identity
from .request_validation import validate as _validate
from .service import reference_eval as reference_eval
from .service import shared_mark as _mark
from .strategy_catalog import get_proof_adapter_ids as get_proof_adapter_ids
from .strategy_catalog import get_strategy_axes as get_strategy_axes
from .strategy_catalog import get_strategy_registry as get_strategy_registry

_identity = identity

# Shared numerical operations used by measurement and tuning.
math = _math

_Obj = dict[str, Any]
_Axes = dict[str, Axis]
_Marks = dict[str, float]

_Summary = tuple[float, float, float, float, float, bool, bool]
_ScalarAdmission = Callable[[_Obj, ResearchSettings], "str | None"]

SHORTFALL_REASON = "Свободные средства недостаточны для маржи."
UNEXPOSED_REASON = "Открытого спреда не было на обучении."


def _as_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return datetime.combine(date.fromisoformat(value), time.min)


def _annualized(
    initial: float,
    end_wealth: float,
    end_price: float,
    anchor_price: float | None,
    start: str,
    end: str,
) -> _Obj | str:
    """Annualized strategy and LQDT returns, or the reason no finite estimate exists."""
    if (
        anchor_price is None
        or not math.isfinite(anchor_price)
        or anchor_price <= 0
        or not math.isfinite(end_price)
        or end_price <= 0
    ):
        return "Непригодная отметка LQDT в расчётном периоде."
    elapsed_days = (_as_datetime(end) - _as_datetime(start)).total_seconds() / 86400
    if elapsed_days <= 0 or min(initial, end_wealth) <= 0:
        return "Нет положительного капитала или календарного интервала для годовой доходности."
    power = 365 / elapsed_days
    try:
        strategy = 100 * math.expm1(power * math.log(end_wealth / initial))
        benchmark = 100 * math.expm1(power * math.log(end_price / anchor_price))
        excess = strategy - benchmark
        if not all(math.isfinite(v) for v in (strategy, benchmark, excess)):
            raise OverflowError
    except (OverflowError, ValueError):
        return "Годовая оценка не является конечным числом."
    return {
        "strategy_annualized_pct": strategy,
        "lqdt_annualized_pct": benchmark,
        "annualized_excess_pp": 0.0 if abs(excess) < 1e-10 else excess,
        "calendar_days": round(elapsed_days, 4),
    }


def measure_scalar_train(
    proof: _Obj,
    marks: _Marks,
    split: EvaluationSplit,
    settings: ResearchSettings,
    admit: _ScalarAdmission,
) -> _Obj:
    """Score one metrics-only training replay; missing evidence is never a zero."""
    if reason := admit(proof, settings):
        return {"utility": None, "reason": reason}
    summary: _Summary = proof["_scalar_train_summary"]
    initial, p0, end_price, end_wealth, end_cum_net, shortfall, exposed = summary
    start, end = split.training[0], split.training[-1]
    train_x = end_wealth - initial * end_price / p0
    train_rate = train_x / len(split.training)
    if not all(math.isfinite(v) for v in (train_rate, train_x)):
        return {"utility": None, "reason": "Нет конечной оценки результата."}
    metrics: _Obj = {
        "net_rub": round(end_cum_net, 2),
        "lqdt_on_margin_rub": round(end_cum_net - train_x, 2),
        "train": round(train_rate, 6),
        "validation": None,
        "excess_rub": round(train_x, 2),
        "rub_per_session": round(train_rate, 6),
    }
    reasons = [] if exposed else [UNEXPOSED_REASON]
    annual = _annualized(initial, end_wealth, end_price, marks.get(start), start, end)
    if isinstance(annual, str):
        return {
            **metrics,
            "utility": None,
            "rank_score": None,
            "reason": " ".join([*reasons, annual]),
        }
    if shortfall:
        # A failed cash path keeps the descriptive metrics and blocks only the score.
        return {
            **metrics,
            "rank_score": None,
            "utility": None,
            "reason": " ".join([*reasons, SHORTFALL_REASON]),
        }
    score = annual["annualized_excess_pp"]
    return {
        **metrics,
        **annual,
        "rank_score": score,
        "utility": None if reasons else score,
        **({"reason": " ".join(reasons)} if reasons else {}),
    }


SEED = 41


def resolve_seed(configured: int | None) -> int:
    """Turn the global seed preference into the base seed of this run.

    A configured seed of 0 means "no fixed seed": every run draws a fresh base
    seed so repeated runs explore different parameter proposals. Any other value
    is used verbatim, which makes every random choice of the run reproducible.
    """
    if configured is None:
        return SEED
    if type(configured) is not int or configured < 0:
        raise W0Error("Сид должен быть целым неотрицательным числом.")
    if configured == 0:
        return secrets.randbelow(2_147_483_647) + 1
    return configured


def derive_seed(base: int, *parts: object) -> int:
    """Derive a stable child seed from the run's base seed and a label.

    Independent searches inside one run (one per strategy family) must not
    propose the same parameter sequence, yet they must stay reproducible
    together: the child seed depends only on the base seed and the label.
    """
    digest = hashlib.sha256(
        "|".join([str(base), *(str(part) for part in parts)]).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:4], "big") or 1


type Numeric = int | float
type Point = dict[str, Numeric]


class _MeasuredLandscape(Protocol):
    def measure(self, values: Point) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class TuningOptions:
    trials: int
    on_progress: Callable[[int, int], None] | None
    random_seed: int | None
    seed_label: object | None


def _suggest_point(
    trial: optuna.trial.Trial,
    seed: Mapping[str, Numeric],
    strategy_axes: Mapping[str, Axis],
    bounds: Mapping[str, Numeric],
) -> Point:
    point: Point = dict(seed)
    for name, axis in strategy_axes.items():
        high = (
            min(bounds[name], round(point["entry_z"] - axis.step, 6))
            if name == "exit_z"
            else bounds[name]
        )
        point[name] = axis.suggest(trial, name, high)
    return point


def run_tuning(
    land: _MeasuredLandscape,
    seed: Mapping[str, Numeric],
    strategy_axes: Mapping[str, Axis],
    train_size: int,
    options: TuningOptions,
) -> tuple[Point | None, int, list[Point]]:
    base = resolve_seed(options.random_seed)
    if options.seed_label is not None:
        base = derive_seed(base, options.seed_label)
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=base, n_startup_trials=8),
    )
    bounds: dict[str, Numeric] = {
        name: min(axis.high, train_size - 2) if name in ("lookback", "window") else axis.high
        for name, axis in strategy_axes.items()
    }
    if all(
        a.contains(seed[k])
        and seed[k] <= bounds[k]
        and math.isclose((seed[k] - a.low) / a.step, round((seed[k] - a.low) / a.step))
        for k, a in strategy_axes.items()
    ):
        study.enqueue_trial({k: seed[k] for k in strategy_axes})
    best: Point | None = None
    best_score = -math.inf
    attempts: list[Point] = []
    for idx in range(options.trials):
        trial = study.ask()
        point = _suggest_point(trial, seed, strategy_axes, bounds)
        measured = land.measure(point)
        raw_score = measured.get("rank_score", measured["utility"])
        if raw_score is None:
            study.tell(trial, state=optuna.trial.TrialState.PRUNED)
        else:
            assert isinstance(raw_score, (int, float))
            score = float(raw_score)
            study.tell(trial, score)
            if score > best_score or (score == best_score and point == seed):
                best, best_score = point, score
        attempts.append(point)
        if options.on_progress is not None:
            options.on_progress(idx + 1, options.trials)
    return best, len(study.trials), attempts


def measure_full(
    proof: _Obj,
    marks: _Marks,
    split: EvaluationSplit,
    settings: ResearchSettings,
) -> _Obj:
    """Measure a continuous ledger; the last training mark is validation's opening balance."""
    if "_scalar_train_summary" in proof and not split.validation:
        return _measure_scalar_train(proof, marks, split, settings)
    if reason := _admission(proof, settings):
        return {"utility": None, "reason": reason}
    curve = _excess_curve(proof, marks)
    train_x = curve[split.training[-1]]
    train_rate = train_x / len(split.training)
    result_x, rate, validation_rate = train_x, train_rate, None
    segments = [("на обучении", split.training)]
    if split.validation:
        result_x = curve[split.validation[-1]] - train_x
        rate = validation_rate = result_x / len(split.validation)
        segments.append(("на проверке", split.validation))
    if not all(math.isfinite(v) for v in (train_rate, rate, result_x)):
        return {"utility": None, "reason": "Нет конечной оценки результата."}
    # Descriptive P&L is separate from eligibility: a flat, unexposed book is not a plateau.
    net = {r["date"]: r["cumulative_net_rub"] for r in cast(list[dict[str, Any]], proof["ledger"])}
    net_change = (
        net[split.validation[-1]] - net[split.training[-1]]
        if split.validation
        else net[split.training[-1]]
    )
    metrics = {
        "net_rub": round(net_change, 2),
        "lqdt_on_margin_rub": round(net_change - result_x, 2),
        "train": round(train_rate, 6),
        "validation": round(validation_rate, 6) if validation_rate is not None else None,
        "excess_rub": round(result_x, 2),
        "rub_per_session": round(rate, 6),
    }
    missing, reasons = _missing_exposure(proof, segments, split)
    try:
        annual = _annualized_returns(proof, marks, split)
    except W0Error as exc:
        return {
            **metrics,
            "utility": None,
            "rank_score": None,
            "reason": " ".join([*reasons, str(exc)]),
        }
    if annual.pop("cash_shortfall"):
        reasons.append("Свободные средства недостаточны для маржи.")
        return {
            **metrics,
            "rank_score": None,
            "utility": None,
            "reason": " ".join(reasons),
        }
    # An honestly inactive book holds LQDT: include its zero in the fixed denominator.
    # It is not evidence of a stable trading neighborhood. A risk/data failure is NOT zero.
    # Benchmark-relative drawdown/shortfall limits no longer veto a candidate: the
    # measured excess over LQDT is the score, and the risk path stays visible in the
    # proof itself.
    score = annual["annualized_excess_pp"]
    return {
        **metrics,
        **annual,
        "rank_score": score,
        "utility": None if missing else score,
        **({"reason": " ".join(reasons)} if reasons else {}),
    }


def admission(proof: _Obj, settings: ResearchSettings) -> str | None:
    if "min_leg_volume" in proof["window"] and proof["window"]["min_leg_volume"] is None:
        return "Нет наблюдений с положительным объёмом обеих ног."
    if not settings.allow_failed_capital and not proof["window"]["passes_capital_screen"]:
        return "Не пройден стресс-тест капитала."
    if not settings.allow_failed_volume and not proof["window"]["tradable_every_day"]:
        return "Не пройдено правило ликвидности."
    if (
        proof["selection"]["strategy"] == "mean_reversion"
        and proof["instrument"]["family"] == "perpetual_quarterly_same_asset"
        and get_timeframe(proof["selection"].get("timeframe", "1d")).supports_funding_strategy
        and not proof["provenance"].get("near_funding_history_sha256")
    ):
        return "Нет полного ряда SWAPRATE."
    return None


TRIALS = 40
MIN_SESSIONS = 24  # Stability gate only; short histories can still show P&L.


ENTRY = Axis(0.5, 5, 0.1)


def __getattr__(name: str) -> Any:
    """Deprecated module attributes kept for out-of-tree readers; use the accessors."""
    if name == "STRATEGY_REGISTRY":
        return get_strategy_registry()
    if name == "PROOF_ADAPTER_IDS":
        return get_proof_adapter_ids()
    if name == "STRATEGY_AXES":
        return get_strategy_axes()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _admission(proof: _Obj, settings: ResearchSettings) -> str | None:
    return admission(proof, settings)


def _measure_scalar_train(
    proof: _Obj, marks: _Marks, split: EvaluationSplit, settings: ResearchSettings
) -> _Obj:
    return measure_scalar_train(proof, marks, split, settings, _admission)


_measure_provider: Callable[[], Callable[..., _Obj]] | None = None


def _set_measure_provider(provider: Callable[[], Callable[..., _Obj]]) -> None:
    global _measure_provider
    _measure_provider = provider


def _active_measure(
    proof: _Obj, marks: _Marks, split: EvaluationSplit, settings: ResearchSettings
) -> _Obj:
    fn = _measure_provider() if _measure_provider is not None else _measure
    return fn(proof, marks, split, settings)


def _measure(
    proof: _Obj, marks: _Marks, split: EvaluationSplit, settings: ResearchSettings
) -> _Obj:
    """Measure a continuous ledger; validation starts at the last training mark."""
    return measure_full(proof, marks, split, settings)


@measure_phase("optuna_trials")
def _tune(
    land: Landscape,
    seed: Mapping[str, float],
    strategy_axes: Mapping[str, Axis],
    train_size: int,
    *,
    trials: int = TRIALS,
    on_progress: Callable[[int, int], None] | None = None,
    random_seed: int | None = None,
    seed_label: object | None = None,
) -> tuple[dict[str, float] | None, int, list[dict[str, float]]]:
    """Maximize the measured center; stability is descriptive, never a veto or a tie breaker."""
    return run_tuning(
        land,
        seed,
        strategy_axes,
        train_size,
        TuningOptions(trials, on_progress, random_seed, seed_label),
    )


class StudyWindow:
    """One independent account, its own dated split, inputs shared by all parameter trials."""

    def __init__(
        self,
        service: Any,
        request: dict[str, Any],
        settings: ResearchSettings,
        benchmark: tuple[list[str], dict[str, float]],
        calendar: Any = None,
        registry: Any = None,
    ) -> None:
        self.request = request
        self.settings = settings
        self.replay: Callable[..., _Obj] = service.prepare(
            request, calendar=calendar, settings=settings
        )
        # `full` is a cached_property: the expensive seed proof is built once, on
        # first access, and reused for the report winner instead of per trial.
        selection = self.full["selection"]
        self.strategy_axes: _Axes = get_strategy_axes(registry)[selection["strategy"]]
        self.execution_axes: _Axes = execution_axes(selection)
        self.axes: _Axes = self.strategy_axes | self.execution_axes
        self.seed: dict[str, float] = {
            **{k: selection["parameters"][k] for k in self.strategy_axes},
            **{k: selection[k] for k in self.execution_axes},
        }
        if not _ordered(self.seed):
            raise W0Error("Порог выхода должен быть ниже порога входа")
        days = [r["date"] for r in self.full["series"] if r.get("volume_eligible", True)]
        self.split: EvaluationSplit = EvaluationSplit.from_dates(days, settings.validation_percent)
        self.train: EvaluationSplit = EvaluationSplit.from_dates(self.split.training, 0)
        dates, prices = benchmark
        if not dates or dates[0] > self.train.training[0]:
            raise W0Error("Нет отметки LQDT на начало истории.")
        self.marks: _Marks = {
            r["date"]: _mark(dates, prices, r.get("benchmark_mark_date") or r["date"][:10])
            for r in self.full["ledger"]
        }

        def evaluate(point: dict[str, float]) -> _Obj:
            return _active_measure(
                self.proof(point, training=True), self.marks, self.train, self.settings
            )

        self.fit: Landscape = Landscape(self.axes, evaluate)

    @cached_property
    def full(self) -> _Obj:
        return self.replay(self.request)

    def proof(self, point: Mapping[str, float], *, training: bool) -> _Obj:
        prefix = training and bool(self.split.validation)
        if point == self.seed and not prefix:
            return self.full
        req: _Obj = {
            **self.request,
            **{k: point[k] for k in self.execution_axes},
            "parameters": {k: point[k] for k in self.strategy_axes},
        }
        through = (
            self.train.training[-1]
            if prefix
            else self.full["series"][-1]["date"]
            if training
            else None
        )
        metrics_only = training and not reference_eval()
        scalar_summary = (self.train.training[0], self.train.training[-1]) if metrics_only else None
        return self.replay(
            req,
            through=through,
            metrics_only=metrics_only,
            scalar_summary=scalar_summary,
        )

    def lamp_warning(self) -> str | None:
        n = len(self.split.training)
        if n < MIN_SESSIONS:
            return f"Недостаточно истории для оценки устойчивости: {n} из {MIN_SESSIONS} наблюдений на обучении."
        return None

    def admission(self, min_observations: int = 2) -> str | None:
        n = len(self.split.training)
        for k in ("lookback", "window"):
            if k in self.strategy_axes:
                min_observations = max(min_observations, int(self.strategy_axes[k].low) + 2)
        if n < min_observations:
            return f"Недостаточно наблюдений на обучении: {n} из {min_observations} для допустимого окна сигнала."
        probe = dict(self.seed)
        if "lookback" in probe:
            probe["lookback"] = min(probe["lookback"], len(self.split.training) - 2)
        return _admission(self.proof(probe, training=True), self.settings)

    @measure_phase("final_report")
    def report(self, point: Mapping[str, float]) -> _Obj:
        p = self.proof(point, training=False)
        measured = _active_measure(p, self.marks, self.split, self.settings)
        evaluation: _Obj = {
            **self.settings.metadata(),
            **self.split.metadata(),
            "execution_reference": p.get("execution_reference", False),
            **({"account_checks": account_checks(p)} if automatic(p["selection"]) else {}),
            "failed_screens": [
                name
                for name, key in (
                    ("volume", "tradable_every_day"),
                    ("capital", "passes_capital_screen"),
                )
                if not p["window"][key]
            ],
            "result": {k: v for k, v in measured.items() if k not in ("utility", "rank_score")},
        }
        return {
            **p,
            "proof_run_id": p["run_id"],
            "evaluation": evaluation,
            "run_id": _run_id({"policy": "annual-excess.v1", "proof": p["run_id"], **evaluation}),
        }


# Facade hooks resolved dynamically by `measure_full` and by `proof_search`.
_FACADE_HOOKS = (_measure_scalar_train, _set_measure_provider)


# Typed internal exports used by other proof modules.
shared_admission = _admission
lamps = _lamps
measure = _measure
shared_measure_scalar_train = _measure_scalar_train
ordered = _ordered
run_id = _run_id
set_measure_provider = _set_measure_provider
tune = _tune
validate = _validate
