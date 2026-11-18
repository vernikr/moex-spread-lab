"""Bounded Optuna search and local, one-field-at-a-time diagnostics for execution proof.

The sampler proposes strategy parameters, never execution assumptions. The same proof
simulators evaluate proposals, sensitivity probes and the displayed portfolio.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import optuna

from .proof import ProofService, _benchmark_marks, _mark, _run_id
from .w0 import W0Error

TRIALS = 40
SEED = 41
FINALISTS = 6
GREEN = 70
AMBER = 40
MIN_SESSIONS = 24  # RUB/session, not annualized: at least 12/6/6 observations.


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


def _measure(proof, marks, dates, train, end):
    if reason := _admission(proof):
        return {"utility": None, "reason": reason}
    curve = _excess_curve(proof, marks)
    peak = 0.0
    for value in curve.values():
        peak = max(peak, value)
        if peak - value >= 10_000 or value <= -15_000:
            return {
                "utility": None,
                "reason": "Нарушен лимит просадки или отставания от LQDT.",
            }
    train_x, valid_x = curve[dates[train - 1]], curve[dates[end - 1]]
    a, b = train_x / train, (valid_x - train_x) / (end - train)
    if not all(math.isfinite(v) for v in (a, b)):
        return {"utility": None, "reason": "Нет конечной оценки результата."}
    periods = (
        ("на обучении", dates[0], dates[train - 1]),
        ("на проверке", dates[train], dates[end - 1]),
    )
    missing = [
        label
        for label, start, stop in periods
        if not any(
            t["entry"]["date"] <= stop and t["exit"]["date"] >= start
            for t in proof["trades"]
        )
    ]
    if missing:
        where = (
            f"только {missing[0]}"
            if len(missing) == 1
            else "ни на обучении, ни на проверке"
        )
        return {"utility": None, "reason": f"Открытого спреда не было {where}."}
    return {
        "utility": round(min(a, b), 6),
        "train": round(a, 6),
        "validation": round(b, 6),
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
                score, reason = 0.0, "Центр неположителен на обучении или проверке."
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
    return best, len(study.trials), list(proposals.values())


def research_run(service: ProofService, request: dict[str, Any], *, optimize: bool):
    _validate(request)
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
    dates = [r["date"] for r in full["series"] if r.get("volume_eligible", True)]
    reason = _admission(full)
    if not reason and len(dates) < MIN_SESSIONS:
        reason = f"Недостаточно истории: {len(dates)} наблюдений из необходимых {MIN_SESSIONS}."
    trials, best, proposals, records, periods, confirmation = (
        0,
        None,
        [],
        [],
        None,
        None,
    )

    def at(point):
        return {
            **request,
            **{k: point[k] for k in EXECUTION_AXES},
            "parameters": {k: point[k] for k in strategy_axes},
        }

    if reason:
        fields = {
            k: {"value": v, "score": None, "reason": reason, "neighbors": []}
            for k, v in seed.items()
        }
    else:
        train, end = len(dates) // 2, len(dates) * 3 // 4
        cutoff = dates[end - 1]
        periods = {
            "training": [dates[0], dates[train - 1]],
            "validation": [dates[train], cutoff],
            "confirmation": [dates[end], dates[-1]],
        }
        b_dates, b_prices = _benchmark_marks(
            service.root / "data/benchmark/lqdt-lineage.json"
        )
        marks = {r["date"]: _mark(b_dates, b_prices, r["date"]) for r in full["ledger"]}

        def evaluate(point):
            return _measure(replay(at(point), through=cutoff), marks, dates, train, end)

        land = Landscape(axes, evaluate)
        if optimize:
            best, trials, proposals = _tune(land, seed, strategy_axes, train)
        fields = {k: land.profile(best or seed, k) for k in axes}
        if optimize:
            records = [
                {"parameters": {k: p[k] for k in strategy_axes}, **land.measure(p)}
                for p in proposals
            ]
            if best is not None:
                # Descriptive only: never use the final quarter to select or veto the candidate.
                curve = _excess_curve(full if best == seed else replay(at(best)), marks)
                confirmation = round(
                    (curve[dates[-1]] - curve[cutoff]) / (len(dates) - end), 6
                )
    plateau = {"fields": fields, "periods": periods, "green": GREEN, "amber": AMBER}
    if not optimize:
        return {**full, "plateau": plateau}
    result = {
        "schema_version": "proof-search.v1",
        "method": "Optuna TPESampler",
        "library_version": optuna.__version__,
        "seed": SEED,
        "trial_count": trials,
        "candidates": records,
        "input_run_id": full["run_id"],
        "selection": selection,
        "selected_parameters": {k: best[k] for k in strategy_axes} if best else None,
        "status": "unavailable"
        if reason
        else "plateau_found"
        if best
        else "no_stable_positive_plateau",
        "reason": reason,
        "plateau": plateau,
        "confirmation_rub_per_session": confirmation,
    }
    return {**result, "run_id": _run_id(result)}
