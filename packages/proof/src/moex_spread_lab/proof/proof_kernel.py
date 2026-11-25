"""Bounded Optuna search and local, one-field-at-a-time diagnostics for execution proof.

The sampler proposes strategy parameters, never execution assumptions. The same proof
simulators evaluate proposals, sensitivity probes and the displayed portfolio.
"""

from __future__ import annotations

import math as _math
import sys
from collections.abc import Callable, Mapping
from datetime import date as date
from datetime import datetime as datetime
from datetime import time as time
from functools import cached_property
from typing import Any, cast

from moex_spread_lab.accounting.position_sizing import automatic
from moex_spread_lab.core import w0 as _core_w0
from moex_spread_lab.core.evaluation_periods import (
    EvaluationSplit,
    ResearchSettings,
)
from moex_spread_lab.core.timeframes import get_timeframe as get_timeframe
from moex_spread_lab.core.w0 import W0Error

from . import axis as _axis
from . import evaluation as _evaluation
from . import landscape as _landscape
from . import measure_full as _measure_full
from . import phase_metrics as _phase_metrics
from . import proof_seed as _proof_seed
from . import request_validation as _request_validation
from . import service as _service
from . import strategy_catalog as _strategy_catalog
from . import tuning as _tuning
from .axis import Axis
from .landscape import AMBER as AMBER
from .landscape import GREEN as GREEN
from .landscape import Landscape

# `measure_full` reads `math` back through this module, the facade it is given.
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


# Callable facade types retain the documented monkeypatch boundary.
_RunId = Callable[[_Obj], str]
_Ordered = Callable[[Mapping[str, float]], bool]
_AnnualizedReturns = Callable[[_Obj, _Marks, EvaluationSplit], dict[str, Any]]
_ExcessCurve = Callable[[_Obj, _Marks], dict[str, float]]
_MissingExposure = Callable[
    [_Obj, list[tuple[str, tuple[str, ...]]], EvaluationSplit], tuple[list[str], list[str]]
]
_AccountChecks = Callable[[_Obj], dict[str, Any]]
_Lamps = Callable[..., dict[str, Any]]
_Identity = Callable[[Mapping[str, Any]], _Obj]
_Validate = Callable[..., None]
_CompactResult = Callable[[_Obj], _Obj]
_ExecutionAxes = Callable[[Mapping[str, Any]], _Axes]
_Mark = Callable[[list[str], _Marks, str], float]
_ReferenceEval = Callable[[], bool]
_GetAdapterIds = Callable[..., frozenset[str]]
_GetStrategyAxes = Callable[..., dict[str, _Axes]]
_GetStrategyRegistry = Callable[..., Any]
_RunTuning = Callable[..., tuple[dict[str, float] | None, int, list[dict[str, float]]]]
_Admission = Callable[[_Obj, ResearchSettings, Any], str | None]
_MeasureFull = Callable[[_Obj, _Marks, EvaluationSplit, ResearchSettings, Any], _Obj]
_MeasurePhase = Callable[[str], Callable[[Callable[..., Any]], Callable[..., Any]]]
_ResolveSeed = Callable[[int | None], int]
_DeriveSeed = Callable[..., int]

_run_id = cast(_RunId, _core_w0.__dict__["_run_id"])
_ordered = cast(_Ordered, _axis.__dict__["_ordered"])
_annualized_returns = cast(_AnnualizedReturns, _evaluation.__dict__["_annualized_returns"])
_excess_curve = cast(_ExcessCurve, _evaluation.__dict__["_excess_curve"])
_missing_exposure = cast(_MissingExposure, _evaluation.__dict__["_missing_exposure"])
account_checks = cast(_AccountChecks, _evaluation.__dict__["account_checks"])
_lamps = cast(_Lamps, _landscape.__dict__["_lamps"])
_identity = cast(_Identity, _request_validation.__dict__["_identity"])
_validate = cast(_Validate, _request_validation.__dict__["_validate"])
compact_result = cast(_CompactResult, _request_validation.__dict__["compact_result"])
execution_axes = cast(_ExecutionAxes, _request_validation.__dict__["execution_axes"])
EXECUTION_AXES: _Axes = cast(_Axes, _request_validation.__dict__["EXECUTION_AXES"])
_mark = cast(_Mark, _service.__dict__["_mark"])
reference_eval = cast(_ReferenceEval, _service.__dict__["reference_eval"])
get_proof_adapter_ids = cast(_GetAdapterIds, _strategy_catalog.__dict__["get_proof_adapter_ids"])
get_strategy_axes = cast(_GetStrategyAxes, _strategy_catalog.__dict__["get_strategy_axes"])
get_strategy_registry = cast(
    _GetStrategyRegistry, _strategy_catalog.__dict__["get_strategy_registry"]
)
TuningOptions = _tuning.TuningOptions
run_tuning = cast(_RunTuning, _tuning.__dict__["run_tuning"])
measure_phase = cast(_MeasurePhase, _phase_metrics.__dict__["measure_phase"])
measure_full = cast(_MeasureFull, _measure_full.__dict__["measure_full"])
admission = cast(_Admission, _measure_full.__dict__["admission"])
SEED = cast(int, _proof_seed.__dict__["SEED"])
resolve_seed = cast(_ResolveSeed, _proof_seed.__dict__["resolve_seed"])
derive_seed = cast(_DeriveSeed, _proof_seed.__dict__["derive_seed"])

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
    return admission(proof, settings, sys.modules[__name__])


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
    return measure_full(proof, marks, split, settings, sys.modules[__name__])


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
