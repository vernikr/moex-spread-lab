"""Bounded Optuna search and local, one-field-at-a-time diagnostics for execution proof.

The sampler proposes strategy parameters, never execution assumptions. The same proof
simulators evaluate proposals, sensitivity probes and the displayed portfolio.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

import optuna

from .evaluation_periods import EvaluationSplit, ResearchSettings
from .portfolio import lqdt_overlay_values
from .position_sizing import AUTO_SIZING, automatic
from .proof import ProofService, _benchmark_marks, _mark, _run_id
from .w0 import W0Error

TRIALS = 40
SEED = 41
GREEN = 70
AMBER = 40
MIN_SESSIONS = 24  # Stability gate only; short histories can still show P&L.


@dataclass(frozen=True)
class Axis:
    low: float
    high: float
    step: float

    def contains(self, value):
        return (
            type(value) in (int, float)
            and self.low <= value <= self.high
            and math.isfinite(value)
            and (self.step != 1 or type(value) is int)
        )

    def suggest(self, trial, name, high):
        if self.step == 1:
            return trial.suggest_int(name, self.low, high)
        return round(trial.suggest_float(name, self.low, high, step=self.step), 6)


ENTRY = Axis(0.5, 5, 0.1)
STRATEGY_AXES = {
    "mean_reversion": {
        "lookback": Axis(5, 80, 1),
        "entry_z": ENTRY,
        "exit_z": Axis(0, 4, 0.1),
    },
    "funding_only": {"lookback": Axis(5, 80, 1)},
    "zscore_mean_reversion": {
        "window": Axis(3, 60, 1),
        "entry_z": ENTRY,
        "exit_z": Axis(0.1, 4, 0.1),
    },
    "convergence_to_ruonia": {"threshold_pp": Axis(0.1, 10, 0.1)},
}
# Fixed-size API/CLI compatibility only. The workbench uses automatic sizing and
# one immutable global slippage preference, so neither is an optimization axis.
EXECUTION_AXES = {"quantity": Axis(1, 50, 1), "slippage_ticks": Axis(0, 10, 0.5)}


def execution_axes(request):
    return {} if automatic(request) else EXECUTION_AXES


def _ordered(values):
    return (
        "exit_z" not in values
        or "entry_z" not in values
        or values["exit_z"] < values["entry_z"]
    )


def _validate(request):
    if request.get("sizing") not in (None, AUTO_SIZING):
        raise W0Error("Неизвестный режим расчёта размера позиции.")
    if automatic(request) and request.get("quantity") is not None:
        raise W0Error("При автоматическом размере количество не задаётся вручную.")
    strategy = request.get("strategy", "zscore_mean_reversion")
    if not isinstance(strategy, str) or strategy not in STRATEGY_AXES:
        raise W0Error("Неизвестная стратегия подбора")
    params = request.get("parameters", {})
    axes = STRATEGY_AXES[strategy]
    if not isinstance(params, dict) or params.keys() - axes.keys():
        raise W0Error("Переданы параметры другой стратегии")
    domains = axes | EXECUTION_AXES
    values = {**params, **{k: request[k] for k in EXECUTION_AXES if k in request and (not automatic(request) or k != "quantity")}}
    for name, value in values.items():
        axis = domains[name]
        if not axis.contains(value):
            raise W0Error(f"{name}: допустимый диапазон {axis.low}–{axis.high}")
    if not _ordered(params):
        raise W0Error("Порог выхода должен быть ниже порога входа")


def _admission(proof, settings):
    if (
        "min_leg_volume" in proof["window"]
        and proof["window"]["min_leg_volume"] is None
    ):
        return "Нет наблюдений с положительным объёмом обеих ног."
    if (
        not settings.allow_failed_capital
        and not proof["window"]["passes_capital_screen"]
    ):
        return "Не пройден стресс-тест капитала."
    if not settings.allow_failed_volume and not proof["window"]["tradable_every_day"]:
        return "Не пройдено правило ликвидности."
    if (
        proof["selection"]["strategy"] == "mean_reversion"
        and proof["instrument"]["family"] == "perpetual_quarterly_same_asset"
        and not proof["provenance"].get("near_funding_history_sha256")
    ):
        return "Нет полного ряда SWAPRATE."
    return None


def _excess_curve(proof, marks):
    """Daily proof ledger minus the accrued LQDT hurdle on each open trade's entry margin.

    Allocate the *published* hurdle exactly at exit, including rounding. Unlike using
    realized trades alone this marks positions crossing a split and intra-trade losses.
    """
    ledger = proof["ledger"]
    if automatic(proof.get("selection", {})):
        initial = proof["sizing"]["initial_capital_rub"]
        start = marks[ledger[0]["date"]]
        return {r["date"]: r["account_value_rub"] - initial * marks[r["date"]] / start for r in ledger}
    index = {row["date"]: i for i, row in enumerate(ledger)}
    hurdle_changes = [0.0] * len(ledger)
    for trade in proof["trades"]:
        entry, exit_ = trade["entry"]["date"], trade["exit"]["date"]
        previous = 0.0
        for i in range(index[entry] + 1, index[exit_] + 1):
            accrued = (
                trade["lqdt_on_margin_rub"]
                if i == index[exit_]
                else trade["margin_rub"] * (marks[ledger[i]["date"]] / marks[entry] - 1)
            )
            hurdle_changes[i] += accrued - previous
            previous = accrued
    hurdle, curve = 0.0, {}
    for row, delta in zip(ledger, hurdle_changes):
        hurdle += delta
        curve[row["date"]] = row["cumulative_net_rub"] - hurdle
    return curve


def _annualized_returns(proof, marks, split):
    """Two independently annualized wealth returns on the same dates, in percentage points."""
    from .screener_app import INITIAL_CAPITAL

    start, train_end = split.training[0], split.training[-1]
    end = split.validation[-1] if split.validation else train_end
    rows = [r for r in proof["ledger"] if start <= r["date"] <= end]
    idx = {r["date"]: i for i, r in enumerate(rows)}
    changes = [0.0] * len(rows)
    for t in proof["trades"]:
        # Entry margin is posted after entry and released at exit; account wealth
        # itself never resets at the boundary. Include zero-volume days while held.
        a, b = t["entry"]["date"], t["exit"]["date"]
        if a in idx:
            changes[idx[a]] += t["margin_rub"]
        if b in idx:
            changes[idx[b]] -= t["margin_rub"]
    margin, margins = 0.0, []
    for delta in changes:
        margin += delta
        margins.append(margin)
    prices = [marks[r["date"]] for r in rows]
    if not all(math.isfinite(v) and v > 0 for v in prices):
        raise W0Error("Непригодная отметка LQDT в расчётном периоде.")
    if automatic(proof.get("selection", {})):
        values = [r["account_value_rub"] for r in rows]
        shortfalls = any(r["cash_shortfall"] for r in rows)
    else:
        values, shortfalls = lqdt_overlay_values(
            INITIAL_CAPITAL, [r["cumulative_net_rub"] for r in rows], margins, prices
        )
    anchor = train_end if split.validation else start
    elapsed = (date.fromisoformat(end) - date.fromisoformat(anchor)).days
    opening = values[idx[anchor]] if split.validation else INITIAL_CAPITAL
    if elapsed <= 0 or min(opening, values[-1]) <= 0:
        raise W0Error(
            "Нет положительного капитала или календарного интервала для годовой доходности."
        )
    power = 365 / elapsed
    try:
        strategy = 100 * math.expm1(power * math.log(values[-1] / opening))
        benchmark = 100 * math.expm1(power * math.log(prices[-1] / prices[idx[anchor]]))
        excess = strategy - benchmark
        if not all(math.isfinite(v) for v in (strategy, benchmark, excess)):
            raise OverflowError
    except (OverflowError, ValueError) as exc:
        raise W0Error("Годовая оценка не является конечным числом.") from exc
    return {
        "strategy_annualized_pct": strategy,
        "lqdt_annualized_pct": benchmark,
        "annualized_excess_pp": 0.0 if abs(excess) < 1e-10 else excess,
        "calendar_days": elapsed,
        "cash_shortfall": bool(shortfalls),
    }


def _measure(proof, marks, split: EvaluationSplit, settings: ResearchSettings):
    """Measure a continuous ledger; the last training mark is validation's opening balance."""
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
    net = {r["date"]: r["cumulative_net_rub"] for r in proof["ledger"]}
    net_change = (
        net[split.validation[-1]] - net[split.training[-1]]
        if split.validation
        else net[split.training[-1]]
    )
    metrics = {
        "net_rub": round(net_change, 2),
        "lqdt_on_margin_rub": round(net_change - result_x, 2),
        "train": round(train_rate, 6),
        "validation": round(validation_rate, 6)
        if validation_rate is not None
        else None,
        "excess_rub": round(result_x, 2),
        "rub_per_session": round(rate, 6),
    }
    missing = [
        label
        for label, days in segments
        if not any(
            t["entry"]["date"] <= days[-1] and t["exit"]["date"] >= days[0]
            for t in proof["trades"]
        )
    ]
    reasons = []
    if missing:
        where = (
            "ни на обучении, ни на проверке"
            if len(missing) == 2
            else f"только {missing[0]}"
            if split.validation
            else missing[0]
        )
        reasons.append(f"Открытого спреда не было {where}.")
    risk_rejected = False
    peak = 0.0
    for value in curve.values():
        peak = max(peak, value)
        if peak - value >= 10_000 or value <= -15_000:
            risk_rejected = True
            reasons.append("Нарушен лимит просадки или отставания от LQDT.")
            break
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
    score = None if risk_rejected else annual["annualized_excess_pp"]
    return {
        **metrics,
        **annual,
        "rank_score": score,
        "utility": None if missing else score,
        **({"reason": " ".join(reasons)} if reasons else {}),
    }


class Landscape:
    """Per-request cache of small measurements, never a cache of candidate chart payloads."""

    def __init__(self, axes, evaluate):
        self.axes = axes
        self.evaluate = evaluate
        self.cache = {}

    def measure(self, values):
        key = tuple(values[name] for name in self.axes)
        if key not in self.cache:
            try:
                self.cache[key] = self.evaluate(values)
            except (
                W0Error
            ) as exc:  # e.g. infeasible adjacent size / insufficient carry warm-up
                self.cache[key] = {"utility": None, "reason": str(exc)}
        return self.cache[key]

    def profile(self, values, name):
        axis, value = self.axes[name], values[name]
        base = self.measure(values)
        readings = []
        for offset in (-2, -1, 1, 2):
            neighbor = round(value + offset * axis.step, 6)
            other = {**values, name: neighbor}
            if axis.contains(neighbor) and _ordered(other):
                readings.append({"value": neighbor, **self.measure(other)})
        valid = [r["utility"] for r in readings if r["utility"] is not None]
        # No evidence is not a red zero; a measured losing center cannot be a positive plateau.
        score, reason, worst = None, base.get("reason"), None
        if base["utility"] is not None and valid:
            worst = min(valid)
            if base["utility"] <= 0:
                score, reason = (
                    0.0,
                    "Результат текущего значения на обучении неположителен.",
                )
            elif len(valid) != len(readings):
                score, reason = 0.0, "Есть сосед без допустимого результата."
            else:
                score = round(100 * min(1, max(0, worst / base["utility"])), 2)
        elif not reason:
            reason = "Нет допустимых соседних значений для сравнения."
        return {
            "value": value,
            "score": score,
            "center": base,
            "neighbors": readings,
            "worst": worst,
            "reason": reason,
            "step": axis.step,
        }


def _tune(land, seed, strategy_axes, train_size):
    """Maximize the measured center; stability is descriptive, never a veto or a tie breaker."""
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=SEED, n_startup_trials=8),
    )
    bounds = {
        name: min(axis.high, train_size - 2)
        if name in ("lookback", "window")
        else axis.high
        for name, axis in strategy_axes.items()
    }
    if all(
        a.contains(seed[k])
        and seed[k] <= bounds[k]
        and math.isclose((seed[k] - a.low) / a.step, round((seed[k] - a.low) / a.step))
        for k, a in strategy_axes.items()
    ):
        study.enqueue_trial({k: seed[k] for k in strategy_axes})
    best, best_score = None, -math.inf
    attempts = []
    for _ in range(TRIALS):
        trial = study.ask()
        point = dict(seed)
        for name, axis in strategy_axes.items():
            high = (
                min(bounds[name], round(point["entry_z"] - axis.step, 6))
                if name == "exit_z"
                else bounds[name]
            )
            point[name] = axis.suggest(trial, name, high)
        measured = land.measure(point)
        score = measured.get("rank_score", measured["utility"])
        if score is None:
            study.tell(trial, state=optuna.trial.TrialState.PRUNED)
        else:
            study.tell(trial, score)
            if score > best_score or (score == best_score and point == seed):
                best, best_score = point, score
        attempts.append(point)
    return best, len(study.trials), attempts


class StudyWindow:
    """One independent account, its own dated split, inputs shared by all parameter trials."""

    def __init__(
        self, service, request, settings: ResearchSettings, benchmark, calendar=None
    ):
        self.request = request
        self.settings = settings
        self.replay = service.prepare(request, calendar=calendar, settings=settings)
        self.full = self.replay(request)
        selection = self.full["selection"]
        self.strategy_axes = STRATEGY_AXES[selection["strategy"]]
        self.execution_axes = execution_axes(selection)
        self.axes = self.strategy_axes | self.execution_axes
        self.seed = {
            **{k: selection["parameters"][k] for k in self.strategy_axes},
            **{k: selection[k] for k in self.execution_axes},
        }
        if not _ordered(self.seed):
            raise W0Error("Порог выхода должен быть ниже порога входа")
        days = [
            r["date"] for r in self.full["series"] if r.get("volume_eligible", True)
        ]
        self.split = EvaluationSplit.from_dates(days, settings.validation_percent)
        self.train = EvaluationSplit.from_dates(self.split.training, 0)
        dates, prices = benchmark
        if not dates or dates[0] > self.train.training[0]:
            raise W0Error("Нет отметки LQDT на начало истории.")
        self.marks = {
            r["date"]: _mark(dates, prices, r["date"]) for r in self.full["ledger"]
        }
        self.fit = Landscape(
            self.axes,
            lambda point: _measure(
                self.proof(point, training=True), self.marks, self.train, self.settings
            ),
        )

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
        return self.replay(req, through=through)

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

    def report(self, point):
        p = self.proof(point, training=False)
        measured = _measure(p, self.marks, self.split, self.settings)
        evaluation = {
            **self.settings.metadata(),
            **self.split.metadata(),
            "failed_screens": [
                name
                for name, key in (
                    ("volume", "tradable_every_day"),
                    ("capital", "passes_capital_screen"),
                )
                if not p["window"][key]
            ],
            "result": {
                k: v for k, v in measured.items() if k not in ("utility", "rank_score")
            },
        }
        return {
            **p,
            "proof_run_id": p["run_id"],
            "evaluation": evaluation,
            "run_id": _run_id(
                {"policy": "annual-excess.v1", "proof": p["run_id"], **evaluation}
            ),
        }


def _lamps(land, point, reason=None):
    fields = (
        {
            k: {"value": point[k], "score": None, "reason": reason, "neighbors": []}
            for k in land.axes
        }
        if reason
        else {k: land.profile(point, k) for k in land.axes}
    )
    return {
        "basis": "training",
        "unit": "annualized_excess_pp",
        "fields": fields,
        "green": GREEN,
        "amber": AMBER,
    }


def research_run(
    service: ProofService,
    request: dict[str, Any],
    *,
    optimize: bool,
    include_plateau: bool = True,
    check_cancelled=lambda: None,
):
    _validate(request)
    settings = ResearchSettings.load(service.root)
    benchmark = _benchmark_marks(service.root / "data/benchmark/lqdt-lineage.json")
    if optimize:
        from .proof_batch import batch_search

        return batch_search(service, request, settings, benchmark, check_cancelled)
    if "targets" in request:
        raise W0Error("Журнал исполнения относится к одному открытому окну.")
    case = StudyWindow(service, request, settings, benchmark)
    report = case.report(case.seed)
    if include_plateau:
        report["plateau"] = _lamps(
            case.fit, case.seed, case.admission() or case.lamp_warning()
        )
    return report
