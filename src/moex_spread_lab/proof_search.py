"""Bounded Optuna search and local, one-field-at-a-time diagnostics for execution proof.

The sampler proposes strategy parameters, never execution assumptions. The same proof
simulators evaluate proposals, sensitivity probes and the displayed portfolio.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import optuna

from .evaluation_periods import EvaluationSplit, read_settings
from .proof import ProofService, _benchmark_marks, _mark, _run_id
from .w0 import W0Error

TRIALS = 40
SEED = 41
FINALISTS = 6
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
EXECUTION_AXES = {"quantity": Axis(1, 50, 1), "slippage_ticks": Axis(0, 10, 0.5)}


def _ordered(values):
    return (
        "exit_z" not in values
        or "entry_z" not in values
        or values["exit_z"] < values["entry_z"]
    )


def _validate(request):
    strategy = request.get("strategy", "zscore_mean_reversion")
    if not isinstance(strategy, str) or strategy not in STRATEGY_AXES:
        raise W0Error("Неизвестная стратегия подбора")
    params = request.get("parameters", {})
    axes = STRATEGY_AXES[strategy]
    if not isinstance(params, dict) or params.keys() - axes.keys():
        raise W0Error("Переданы параметры другой стратегии")
    domains = axes | EXECUTION_AXES
    values = {**params, **{k: request[k] for k in EXECUTION_AXES if k in request}}
    for name, value in values.items():
        axis = domains[name]
        if not axis.contains(value):
            raise W0Error(f"{name}: допустимый диапазон {axis.low}–{axis.high}")
    if not _ordered(params):
        raise W0Error("Порог выхода должен быть ниже порога входа")


def _admission(proof):
    if not proof["window"]["passes_capital_screen"]:
        return "Не пройден стресс-тест капитала."
    if not proof["window"]["tradable_every_day"]:
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


def _measure(proof, marks, split: EvaluationSplit):
    """Measure a continuous ledger; the last training mark is validation's opening balance."""
    if reason := _admission(proof):
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
    peak = 0.0
    for value in curve.values():
        peak = max(peak, value)
        if peak - value >= 10_000 or value <= -15_000:
            reasons.append("Нарушен лимит просадки или отставания от LQDT.")
            break
    if reasons:
        return {**metrics, "utility": None, "reason": " ".join(reasons)}
    return {**metrics, "utility": metrics["rub_per_session"]}


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
    """TPE proposes joint, stepped configurations; actual 1D probes reject narrow peaks."""
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
        seed[k] <= bounds[k]
        and math.isclose((seed[k] - a.low) / a.step, round((seed[k] - a.low) / a.step))
        for k, a in strategy_axes.items()
    ):
        study.enqueue_trial({k: seed[k] for k in strategy_axes})
    proposals = {tuple(seed[k] for k in strategy_axes): seed}
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
        score = land.measure(point)["utility"]
        if score is None:
            study.tell(trial, state=optuna.trial.TrialState.PRUNED)
        else:
            study.tell(trial, score)
        attempts.append(point)
        proposals[tuple(point[k] for k in strategy_axes)] = point

    candidates = [
        p for p in proposals.values() if (land.measure(p)["utility"] or 0) > 0
    ]
    strongest = sorted(
        candidates, key=lambda p: land.measure(p)["utility"], reverse=True
    )[:FINALISTS]
    if seed in candidates and seed not in strongest:
        strongest.append(seed)
    best, best_floor = None, 0.0
    for center in strongest:
        profiles = [land.profile(center, name) for name in strategy_axes]
        if all(
            p["score"] is not None and p["score"] >= GREEN and len(p["neighbors"]) >= 2
            for p in profiles
        ):
            floor = min(
                land.measure(center)["utility"], *(p["worst"] for p in profiles)
            )
            if floor > best_floor or (floor == best_floor and center == seed):
                best, best_floor = center, floor
    return best, len(study.trials), attempts


def research_run(
    service: ProofService,
    request: dict[str, Any],
    *,
    optimize: bool,
    include_plateau: bool = True,
):
    _validate(request)
    # Snapshot the project setting once, including for programmatic callers and trial logs.
    percent = read_settings(service.root)["validation_percent"]
    replay = service.prepare(request)
    full = replay(request)
    selection = full["selection"]
    strategy_axes = STRATEGY_AXES[selection["strategy"]]
    axes = strategy_axes | EXECUTION_AXES
    seed = {
        **{k: selection["parameters"][k] for k in strategy_axes},
        **{k: selection[k] for k in EXECUTION_AXES},
    }
    if not _ordered(seed):
        raise W0Error("Порог выхода должен быть ниже порога входа")
    days = [r["date"] for r in full["series"] if r.get("volume_eligible", True)]
    split = EvaluationSplit.from_dates(days, percent)
    b_dates, b_prices = _benchmark_marks(
        service.root / "data/benchmark/lqdt-lineage.json"
    )
    marks = {r["date"]: _mark(b_dates, b_prices, r["date"]) for r in full["ledger"]}

    def at(point):
        return {
            **request,
            **{k: point[k] for k in EXECUTION_AXES},
            "parameters": {k: point[k] for k in strategy_axes},
        }

    def report(p):
        measured = _measure(p, marks, split)
        evaluation = {
            **split.metadata(),
            "result": {k: v for k, v in measured.items() if k != "utility"},
        }
        return {
            **p,
            "proof_run_id": p["run_id"],
            "evaluation": evaluation,
            "run_id": _run_id(
                {"policy": "two-period.v1", "proof": p["run_id"], **evaluation}
            ),
        }

    if not optimize and not include_plateau:
        return report(full)  # No sensitivity replays for a plain API run.

    train = EvaluationSplit.from_dates(split.training, 0)
    cutoff = train.training[-1] if split.validation else full["series"][-1]["date"]

    def training_proof(point):
        if not split.validation and point == seed:
            return full
        return replay(at(point), through=cutoff)

    fit = Landscape(axes, lambda point: _measure(training_proof(point), marks, train))
    best, trials, records = None, 0, []
    reason = None
    if len(days) < MIN_SESSIONS:
        reason = f"Недостаточно истории: {len(days)} наблюдений из необходимых {MIN_SESSIONS}."
    elif not split.validation:
        reason = _admission(full)
    else:
        # Gate on the prefix, not the final window's margin/liquidity or realized outcome.
        # A too-long seed warm-up must not block other feasible Optuna proposals.
        probe = {
            **seed,
            **(
                {"lookback": min(seed["lookback"], len(split.training) - 2)}
                if "lookback" in seed
                else {}
            ),
        }
        prefix = training_proof(probe)
        reason = _admission(prefix)
    if optimize and not reason:
        best, trials, attempts = _tune(fit, seed, strategy_axes, len(split.training))
        records = [
            {
                "trial": i,
                "parameters": {k: p[k] for k in strategy_axes},
                **fit.measure(p),
            }
            for i, p in enumerate(attempts)
        ]
    point = best or seed
    fields = (
        {
            k: {"value": v, "score": None, "reason": reason, "neighbors": []}
            for k, v in point.items()
        }
        if reason
        else {k: fit.profile(point, k) for k in axes}
    )
    # Only after ranking is final, evaluate the current/chosen point on the continuous ledger.
    # Validation never enters TPE, a neighbor check, a fallback choice or an admission to ranking.
    displayed = report(full if point == seed else replay(at(point)))
    plateau = {"basis": "training", "fields": fields, "green": GREEN, "amber": AMBER}
    if not optimize:
        return {**displayed, "plateau": plateau}
    result = {
        "schema_version": "proof-search.v2",
        "method": "Optuna TPESampler",
        "library_version": optuna.__version__,
        "seed": SEED,
        "trial_count": trials,
        "candidates": records,
        "input_run_id": full["run_id"],
        "result_run_id": displayed["run_id"],
        "evaluation": displayed["evaluation"],
        "selection": selection,
        "selected_parameters": {k: best[k] for k in strategy_axes} if best else None,
        "status": "unavailable"
        if reason
        else "plateau_found"
        if best
        else "no_stable_positive_plateau",
        "reason": reason,
        "selection_basis": "training",
        "plateau": plateau,
    }
    return {**result, "run_id": _run_id(result)}
