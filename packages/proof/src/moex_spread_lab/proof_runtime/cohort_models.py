"""Local and worker-backed cohort evaluation and reporting models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from moex_spread_lab.core.w0 import W0Error

from .axis import _ordered
from .cohort_prepare_helpers import _translate
from .landscape import Landscape, _lamps
from .phase_metrics import measure_phase

if TYPE_CHECKING:
    from .proof_pool import CohortWorkerPool


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
    pool: CohortWorkerPool | None = None
    cohort_state: dict[str, Any] | None = None
    distinct_strategies: tuple = ()
    check_cancelled: object = None

    def close(self) -> None:
        if self.pool is not None and self.cohort_state is not None:
            self.pool.release_cohort(self.cohort_state["cohort_id"], self.cohort_state["owner_of"])
            self.cohort_state = None

    def _uncached_points(self, point):
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

        return uncached_points

    @measure_phase("plateau")
    def plateau(self, point, *, on_progress=None):
        reason = self._plateau_reason(point)
        if not reason:
            uncached_points = self._uncached_points(point)

            if uncached_points:
                batch_payload = [
                    (idx, p_by_strat) for idx, (_, p_by_strat) in enumerate(uncached_points)
                ]
                batch_out = cast("CohortWorkerPool", self.pool).eval_plateau_batch(
                    cast(dict[str, Any], self.cohort_state),
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
        results = cast("CohortWorkerPool", self.pool).report_cohort(
            cast(dict[str, Any], self.cohort_state),
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
