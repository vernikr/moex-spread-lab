"""Local and worker-backed cohort evaluation and reporting models."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from moex_spread_lab.core.contracts import plugin_parameter_aliases
from moex_spread_lab.core.settings import EvaluationSplit, ResearchSettings
from moex_spread_lab.core.w0 import W0Error

from .landscape import AMBER, GREEN, Landscape
from .phase_metrics import measure_phase

if TYPE_CHECKING:
    from .proof_pool import CohortWorkerPool


ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], None]
PointValues = Mapping[str, Any]


class _LandscapeView(Protocol):
    @property
    def axes(self) -> Mapping[str, Any]: ...

    @property
    def cache(self) -> dict[tuple[Any, ...], Any]: ...

    def measure(self, values: PointValues) -> Mapping[str, Any]: ...

    def profile(self, values: PointValues, name: str) -> Mapping[str, Any]: ...


class _CohortPool(Protocol):
    def eval_plateau_batch(
        self,
        cohort_state: dict[str, Any],
        points: list[tuple[int, dict[str, dict[str, Any]]]],
        check_cancelled: CancelCheck,
        on_progress: ProgressCallback | None = None,
    ) -> dict[int, dict[str, Any]]: ...

    def report_cohort(
        self,
        cohort_state: dict[str, Any],
        point_by_strategy: dict[str, dict[str, Any]],
        best_is_none: bool,
        settings: ResearchSettings,
        check_cancelled: CancelCheck,
        on_progress: ProgressCallback | None = None,
    ) -> list[tuple[int, dict[str, Any], bool, dict[str, Any] | None]]: ...


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


def _ordered_values(values: PointValues) -> bool:
    return "exit_z" not in values or "entry_z" not in values or values["exit_z"] < values["entry_z"]


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


def _probe_points(land: _LandscapeView, point: PointValues) -> list[dict[str, Any]]:
    probe_points: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    center_key = tuple(point[name] for name in land.axes)
    if center_key not in land.cache:
        seen.add(center_key)
        probe_points.append(dict(point))
    for name, axis in land.axes.items():
        value = point[name]
        for offset in (-2, -1, 1, 2):
            neighbor = round(value + offset * axis.step, 6)
            other = {**point, name: neighbor}
            if axis.contains(neighbor) and _ordered_values(other):
                key = tuple(other[name] for name in land.axes)
                if key not in land.cache and key not in seen:
                    seen.add(key)
                    probe_points.append(other)
    return probe_points


def _plateau_lamps(
    land: _LandscapeView,
    point: PointValues,
    reason: str | None,
    *,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    # Same fields, pre-warmed probes and progress accounting as the landscape helper.
    if reason:
        fields: dict[str, Any] = {
            name: {"value": point[name], "score": None, "reason": reason, "neighbors": []}
            for name in land.axes
        }
    else:
        if on_progress is not None:
            probes = _probe_points(land, point)
            total_probes = len(probes)
            for idx, probe in enumerate(probes, start=1):
                land.measure(probe)
                on_progress(idx, total_probes)
        fields = {name: land.profile(point, name) for name in land.axes}
    return {
        "basis": "training",
        "unit": "annualized_excess_pp",
        "fields": fields,
        "green": GREEN,
        "amber": AMBER,
    }


@dataclass
class _Cohort:
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
        land = cast(_LandscapeView, first.fit if native_single else self.land)
        return _plateau_lamps(land, point, reason, on_progress=on_progress)

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
            training_rate = cast(_LandscapeView, member.fit).measure(local).get("rank_score")
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
class _RemoteCohort(_Cohort):
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
        land = cast(_LandscapeView, self.land)
        uncached_points: list[tuple[tuple[Any, ...], dict[str, dict[str, Any]]]] = []
        seen_keys: set[tuple[Any, ...]] = set()

        def enqueue(candidate: PointValues) -> None:
            key = tuple(candidate[name] for name in land.axes)
            if key not in land.cache and key not in seen_keys:
                seen_keys.add(key)
                point_by_strat = {
                    strat: _translate_parameters(candidate, strat)
                    for strat in self.distinct_strategies
                }
                uncached_points.append((key, point_by_strat))

        enqueue(point)
        for name, axis in land.axes.items():
            value = point[name]
            for offset in (-2, -1, 1, 2):
                neighbor = round(value + offset * axis.step, 6)
                other = {**point, name: neighbor}
                if axis.contains(neighbor) and _ordered_values(other):
                    enqueue(other)

        return uncached_points

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
                pool = cast(_CohortPool, self.pool)
                batch_out = pool.eval_plateau_batch(
                    cast(dict[str, Any], self.cohort_state),
                    batch_payload,
                    cast(CancelCheck, self.check_cancelled),
                    on_progress=on_progress,
                )
                land = cast(_LandscapeView, self.land)
                for idx, (key, _) in enumerate(uncached_points):
                    land.cache[key] = batch_out[idx]
        return _plateau_lamps(cast(_LandscapeView, self.land), point, reason)

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
        results = cast(_CohortPool, self.pool).report_cohort(
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


# Typed internal exports used by other proof modules.
Cohort = _Cohort
RemoteCohort = _RemoteCohort
