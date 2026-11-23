"""Bounded Optuna search and local, one-field-at-a-time diagnostics for execution proof.

The sampler proposes strategy parameters, never execution assumptions. The same proof
simulators evaluate proposals, sensitivity probes and the displayed portfolio.
"""

from __future__ import annotations

import math
import sys
from datetime import date, datetime, time
from functools import cached_property

from moex_spread_lab.accounting.position_sizing import automatic
from moex_spread_lab.core.evaluation_periods import (
    EvaluationSplit,
    ResearchSettings,
)
from moex_spread_lab.core.timeframes import get_timeframe as get_timeframe
from moex_spread_lab.core.w0 import W0Error, _run_id

from .axis import Axis, _ordered
from .evaluation import _annualized_returns as _annualized_returns
from .evaluation import _excess_curve as _excess_curve
from .evaluation import _missing_exposure as _missing_exposure
from .evaluation import account_checks
from .landscape import AMBER as AMBER
from .landscape import GREEN as GREEN
from .landscape import Landscape as Landscape
from .landscape import _lamps as _lamps
from .measure_full import admission, measure_full
from .phase_metrics import measure_phase
from .proof_seed import SEED as SEED
from .proof_seed import derive_seed as derive_seed
from .proof_seed import resolve_seed as resolve_seed
from .request_validation import EXECUTION_AXES as EXECUTION_AXES
from .request_validation import _identity as _identity
from .request_validation import _validate as _validate
from .request_validation import compact_result as compact_result
from .request_validation import execution_axes
from .service import _mark, reference_eval
from .strategy_catalog import get_proof_adapter_ids, get_strategy_axes, get_strategy_registry
from .tuning import TuningOptions, run_tuning

TRIALS = 40
MIN_SESSIONS = 24  # Stability gate only; short histories can still show P&L.


ENTRY = Axis(0.5, 5, 0.1)


def __getattr__(name: str):
    """Deprecated module attributes kept for out-of-tree readers; use the accessors."""
    if name == "STRATEGY_REGISTRY":
        return get_strategy_registry()
    if name == "PROOF_ADAPTER_IDS":
        return get_proof_adapter_ids()
    if name == "STRATEGY_AXES":
        return get_strategy_axes()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _admission(proof, settings):
    return admission(proof, settings, sys.modules[__name__])


def _measure_scalar_train(proof, marks, split: EvaluationSplit, settings: ResearchSettings):
    if reason := _admission(proof, settings):
        return {"utility": None, "reason": reason}
    (
        initial,
        p0,
        end_price,
        end_wealth,
        end_cum_net,
        any_window_shortfall,
        exposed,
    ) = proof["_scalar_train_summary"]
    start, end = split.training[0], split.training[-1]
    train_x = end_wealth - initial * end_price / p0
    train_rate = train_x / len(split.training)
    if not all(math.isfinite(v) for v in (train_rate, train_x)):
        return {"utility": None, "reason": "Нет конечной оценки результата."}
    metrics = {
        "net_rub": round(end_cum_net, 2),
        "lqdt_on_margin_rub": round(end_cum_net - train_x, 2),
        "train": round(train_rate, 6),
        "validation": None,
        "excess_rub": round(train_x, 2),
        "rub_per_session": round(train_rate, 6),
    }
    reasons = []
    missing = False
    if not exposed:
        missing = True
        reasons.append("Открытого спреда не было на обучении.")

    def as_datetime(value):
        try:
            return datetime.fromisoformat(value.replace("Z", ""))
        except ValueError:
            return datetime.combine(date.fromisoformat(value), time.min)

    anchor_price = marks.get(start)
    if (
        anchor_price is None
        or not math.isfinite(anchor_price)
        or anchor_price <= 0
        or not math.isfinite(end_price)
        or end_price <= 0
    ):
        return {
            **metrics,
            "utility": None,
            "rank_score": None,
            "reason": " ".join([*reasons, "Непригодная отметка LQDT в расчётном периоде."]),
        }
    elapsed_days = (as_datetime(end) - as_datetime(start)).total_seconds() / 86400
    if elapsed_days <= 0 or min(initial, end_wealth) <= 0:
        return {
            **metrics,
            "utility": None,
            "rank_score": None,
            "reason": " ".join(
                [
                    *reasons,
                    "Нет положительного капитала или календарного интервала для годовой доходности.",
                ]
            ),
        }
    power = 365 / elapsed_days
    try:
        strategy = 100 * math.expm1(power * math.log(end_wealth / initial))
        benchmark = 100 * math.expm1(power * math.log(end_price / anchor_price))
        excess = strategy - benchmark
        if not all(math.isfinite(v) for v in (strategy, benchmark, excess)):
            raise OverflowError
    except (OverflowError, ValueError):
        return {
            **metrics,
            "utility": None,
            "rank_score": None,
            "reason": " ".join([*reasons, "Годовая оценка не является конечным числом."]),
        }
    if any_window_shortfall:
        reasons.append("Свободные средства недостаточны для маржи.")
        return {
            **metrics,
            "rank_score": None,
            "utility": None,
            "reason": " ".join(reasons),
        }
    annual = {
        "strategy_annualized_pct": strategy,
        "lqdt_annualized_pct": benchmark,
        "annualized_excess_pp": 0.0 if abs(excess) < 1e-10 else excess,
        "calendar_days": round(elapsed_days, 4),
    }
    score = annual["annualized_excess_pp"]
    return {
        **metrics,
        **annual,
        "rank_score": score,
        "utility": None if missing else score,
        **({"reason": " ".join(reasons)} if reasons else {}),
    }


_measure_provider = None


def _set_measure_provider(provider) -> None:
    global _measure_provider
    _measure_provider = provider


def _active_measure(proof, marks, split: EvaluationSplit, settings: ResearchSettings):
    fn = _measure_provider() if _measure_provider is not None else _measure
    return fn(proof, marks, split, settings)


def _measure(proof, marks, split: EvaluationSplit, settings: ResearchSettings):
    """Measure a continuous ledger; validation starts at the last training mark."""
    return measure_full(proof, marks, split, settings, sys.modules[__name__])


@measure_phase("optuna_trials")
def _tune(
    land,
    seed,
    strategy_axes,
    train_size,
    *,
    trials=TRIALS,
    on_progress=None,
    random_seed=None,
    seed_label=None,
):
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
        self, service, request, settings: ResearchSettings, benchmark, calendar=None, registry=None
    ):
        self.request = request
        self.settings = settings
        self.replay = service.prepare(request, calendar=calendar, settings=settings)
        # `full` is a cached_property: the expensive seed proof is built once, on
        # first access, and reused for the report winner instead of per trial.
        selection = self.full["selection"]
        self.strategy_axes = get_strategy_axes(registry)[selection["strategy"]]
        self.execution_axes = execution_axes(selection)
        self.axes = self.strategy_axes | self.execution_axes
        self.seed = {
            **{k: selection["parameters"][k] for k in self.strategy_axes},
            **{k: selection[k] for k in self.execution_axes},
        }
        if not _ordered(self.seed):
            raise W0Error("Порог выхода должен быть ниже порога входа")
        days = [r["date"] for r in self.full["series"] if r.get("volume_eligible", True)]
        self.split = EvaluationSplit.from_dates(days, settings.validation_percent)
        self.train = EvaluationSplit.from_dates(self.split.training, 0)
        dates, prices = benchmark
        if not dates or dates[0] > self.train.training[0]:
            raise W0Error("Нет отметки LQDT на начало истории.")
        self.marks = {
            r["date"]: _mark(dates, prices, r.get("benchmark_mark_date") or r["date"][:10])
            for r in self.full["ledger"]
        }
        self.fit = Landscape(
            self.axes,
            lambda point: _active_measure(
                self.proof(point, training=True), self.marks, self.train, self.settings
            ),
        )

    @cached_property
    def full(self):
        return self.replay(self.request)

    def proof(self, point, *, training):
        prefix = training and self.split.validation
        if point == self.seed and not prefix:
            return self.full
        req = {
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

    def lamp_warning(self):
        n = len(self.split.training)
        if n < MIN_SESSIONS:
            return f"Недостаточно истории для оценки устойчивости: {n} из {MIN_SESSIONS} наблюдений на обучении."
        return None

    def admission(self, min_observations=2):
        n = len(self.split.training)
        for k in ("lookback", "window"):
            if k in self.strategy_axes:
                min_observations = max(min_observations, self.strategy_axes[k].low + 2)
        if n < min_observations:
            return f"Недостаточно наблюдений на обучении: {n} из {min_observations} для допустимого окна сигнала."
        probe = dict(self.seed)
        if "lookback" in probe:
            probe["lookback"] = min(probe["lookback"], len(self.split.training) - 2)
        return _admission(self.proof(probe, training=True), self.settings)

    @measure_phase("final_report")
    def report(self, point):
        p = self.proof(point, training=False)
        measured = _active_measure(p, self.marks, self.split, self.settings)
        evaluation = {
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
