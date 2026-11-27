"""Local and worker-backed cohort evaluation and reporting models."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from moex_spread_lab.core.contracts import plugin_parameter_aliases
from moex_spread_lab.core.settings import EvaluationSplit, ResearchSettings
from moex_spread_lab.core.w0 import W0Error

from .landscape import Landscape, lamps, probe_points
from .phase_metrics import measure_phase

if TYPE_CHECKING:
    from .proof_pool import CohortWorkerPool


ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], None]
PointValues = Mapping[str, Any]


class _MemberCase(Protocol):
    request: PointValues

    def lamp_warning(self) -> str | None: ...


class _LocalMemberCase(_MemberCase, Protocol):
    strategy_axes: Mapping[str, Any]
    execution_axes: Mapping[str, Any]
    axes: Mapping[str, Any]
    fit: Landscape
    split: EvaluationSplit

    @property
    def full(self) -> Mapping[str, Any]: ...

    def report(self, point: PointValues) -> dict[str, Any]: ...


def _translate_parameters(params: PointValues, strategy: str) -> dict[str, Any]:
    """Rename shared parameter names to ``strategy``'s declared spelling."""
    result = dict(params)
    try:
        from .strategy_catalog import resolve_strategy_registry

        plugin = resolve_strategy_registry(None).get(strategy)
    except W0Error:
        return result
    for source, target in plugin_parameter_aliases(plugin).items():
        if source in result:
            result[target] = result.pop(source)
    return result


@dataclass
class Cohort:
    active: str
    axes: Mapping[str, Any]
    seed: Mapping[str, Any]
    members: list[tuple[dict[str, Any], _MemberCase]]
    land: Landscape

    def close(self) -> None:
        pass

    def _plateau_reason(self, point: PointValues) -> str | None:
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
    def plateau(
        self, point: PointValues, *, on_progress: ProgressCallback | None = None
    ) -> dict[str, Any]:
        # Never substitute a clamped seed or a smaller, measurable subset for the user's vector.
        reason = self._plateau_reason(point)
        first = cast(_LocalMemberCase, self.members[0][1])
        native_single = len(self.members) == 1 and first.request["strategy"] == self.active
        land = first.fit if native_single else self.land
        return lamps(land, point, reason, on_progress=on_progress)

    @measure_phase("final_report")
    def report_members(
        self,
        point: PointValues,
        best: bool,
        settings: ResearchSettings,
        check_cancelled: CancelCheck,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[list[dict[str, Any]], bool, dict[str, Any] | None]:
        rows: list[dict[str, Any]] = []
        single: dict[str, Any] | None = None
        any_exec_ref = any(
            cast(_LocalMemberCase, case).full.get("execution_reference", False)
            for _, case in self.members
        )
        total_members = len(self.members)
        for idx, (target, case) in enumerate(self.members, start=1):
            check_cancelled()
            member = cast(_LocalMemberCase, case)
            local = _translate_parameters(point, member.request["strategy"])
            selection: dict[str, Any] = {
                **member.full["selection"],
                **{k: local[k] for k in member.execution_axes},
                "parameters": {k: local[k] for k in member.strategy_axes},
            }
            if not best and any(not a.contains(local[k]) for k, a in member.axes.items()):
                rows.append(
                    {
                        "target": target,
                        "reason": "Исходные параметры не входят в общий диапазон.",
                    }
                )
                if on_progress is not None:
                    on_progress(idx, total_members)
                continue
            training_rate = member.fit.measure(local).get("rank_score")
            row: dict[str, Any] = {
                "target": target,
                "selection": selection,
                "input_run_id": member.full["run_id"],
                "training_annualized_excess_pp": training_rate,
            }
            try:
                single = member.report(local)
                rows.append({**row, "run_id": single["run_id"], "evaluation": single["evaluation"]})
            except (W0Error, OSError) as exc:
                rows.append(
                    {
                        **row,
                        "evaluation": {
                            **settings.metadata(),
                            **member.split.metadata(),
                            "execution_reference": member.full.get("execution_reference", False),
                            "result": {"reason": str(exc)},
                        },
                    }
                )
            if on_progress is not None:
                on_progress(idx, total_members)
        return rows, any_exec_ref, single


@dataclass
class RemoteCohort(Cohort):
    pool: CohortWorkerPool | None = None
    # Worker state stays dynamically typed until its producer is strict-checked;
    # casts below express the contract at the pool boundary.
    cohort_state: Any = None
    distinct_strategies: tuple[str, ...] = ()
    check_cancelled: CancelCheck | None = None

    def close(self) -> None:
        if self.pool is not None and self.cohort_state is not None:
            self.pool.release_cohort(self.cohort_state["cohort_id"], self.cohort_state["owner_of"])
            self.cohort_state = None

    def _uncached_points(
        self, point: PointValues
    ) -> list[tuple[tuple[Any, ...], dict[str, dict[str, Any]]]]:
        return [
            (
                tuple(probe[name] for name in self.land.axes),
                {strat: _translate_parameters(probe, strat) for strat in self.distinct_strategies},
            )
            for probe in probe_points(self.land, point)
        ]

    @measure_phase("plateau")
    def plateau(
        self, point: PointValues, *, on_progress: ProgressCallback | None = None
    ) -> dict[str, Any]:
        reason = self._plateau_reason(point)
        if not reason:
            uncached_points = self._uncached_points(point)

            if uncached_points:
                batch_payload: list[tuple[int, dict[str, dict[str, Any]]]] = [
                    (idx, p_by_strat) for idx, (_, p_by_strat) in enumerate(uncached_points)
                ]
                pool = cast("CohortWorkerPool", self.pool)
                batch_out = pool.eval_plateau_batch(
                    cast(dict[str, Any], self.cohort_state),
                    batch_payload,
                    cast(CancelCheck, self.check_cancelled),
                    on_progress=on_progress,
                )
                for idx, (key, _) in enumerate(uncached_points):
                    self.land.cache[key] = batch_out[idx]
        return lamps(self.land, point, reason)

    @measure_phase("final_report")
    def report_members(
        self,
        point: PointValues,
        best: bool,
        settings: ResearchSettings,
        check_cancelled: CancelCheck,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[list[dict[str, Any]], bool, dict[str, Any] | None]:
        point_by_strat = {
            strat: _translate_parameters(point, strat) for strat in self.distinct_strategies
        }
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
