"""Process pool (spawn) with Sticky Sharding, LPT load balancing, and Canary fail-fast."""

from __future__ import annotations

import atexit
import multiprocessing
import os
import sys
import threading
import uuid
import weakref
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path
from statistics import fmean
from typing import Any

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.funding_carry.carry import CarryStrategyError

from .proof_kernel import StudyWindow
from .strategy_catalog import get_strategy_axes
from .worker_metrics import submit_worker, unwrap_worker_result
from .worker_weight import _estimate_weight
from .worker_window import _worker_service, _worker_window_summaries

MIN_POOL_WINDOWS = 3
MIN_POOL_BARS = 1500
POLL_INTERVAL_SECONDS = 0.05

_WORKER_COHORTS: dict[str, dict[int, tuple[dict[str, Any], StudyWindow]]] = {}


def configured_workers() -> int:
    raw = os.environ.get("MOEX_PROOF_WORKERS")
    if raw is not None:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    cpu = os.cpu_count() or 6
    if sys.platform == "darwin" and cpu > 4:
        return max(2, min(6, cpu // 2))
    return max(1, min(8, cpu))


def _worker_init_cohort(
    cohort_id: str,
    root_str: str,
    settings: ResearchSettings,
    benchmark: tuple[list[str], dict[str, float]],
    items: list[tuple[int, dict[str, Any], dict[str, Any], bool]],
) -> list[tuple[int, str, dict[str, Any]]]:
    """Initialize assigned StudyWindows inside the worker and keep them sticky."""
    svc = _worker_service(root_str)
    calendar = svc._calendar
    bucket = _WORKER_COHORTS.setdefault(cohort_id, {})
    results: list[tuple[int, str, dict[str, Any]]] = []
    for idx, target, native, checked in items:
        try:
            if checked and target["universe"] == CALENDAR_UNIVERSE:
                calendar.check_training_window(
                    target["asset"], target["pair"], native.get("quantity", 1), settings
                )
            # Worker startup is lazy: discovery happens here, not at import time.
            strategy_axes = get_strategy_axes()
            initial = dict(native["parameters"])
            for name, axis in strategy_axes[native["strategy"]].items():
                if name in initial:
                    initial[name] = min(axis.high, max(axis.low, initial[name]))
            try:
                case = StudyWindow(
                    svc,
                    {**native, "parameters": initial},
                    settings,
                    benchmark,
                    calendar,
                )
            except CarryStrategyError:
                if native["strategy"] != "funding_only" or initial["lookback"] == 5:
                    raise
                case = StudyWindow(
                    svc,
                    {**native, "parameters": {"lookback": 5}},
                    settings,
                    benchmark,
                    calendar,
                )
            reason = case.admission()
            if reason:
                results.append((idx, "unavailable" if not checked else "error", {"reason": reason}))
                continue
            bucket[idx] = (target, case)
            results.append(
                (
                    idx,
                    "ok",
                    {
                        "strategy": case.request["strategy"],
                        "strategy_axes": case.strategy_axes,
                        "execution_axes": case.execution_axes,
                        "axes": case.axes,
                        "seed": case.seed,
                        "training_first": case.split.training[0],
                        "training_last": case.split.training[-1],
                        "training_count": len(case.split.training),
                        "lamp_warning": case.lamp_warning(),
                    },
                )
            )
        except (W0Error, OSError) as exc:
            results.append((idx, "error", {"reason": str(exc)}))
    return results


def _worker_release_cohort(cohort_id: str) -> None:
    _WORKER_COHORTS.pop(cohort_id, None)


def _worker_eval_indices(
    cohort_id: str,
    point_by_strategy: dict[str, dict[str, Any]],
    indices: list[int] | None = None,
    exclude_idx: int | None = None,
) -> tuple[tuple[int, str, str] | None, list[tuple[int, float, bool]]]:
    """Evaluate `indices` (or all shard indices) in canonical order; short-circuit on fail."""
    bucket = _WORKER_COHORTS.get(cohort_id, {})
    target_indices = sorted(indices) if indices is not None else sorted(bucket)
    scores: list[tuple[int, float, bool]] = []
    for idx in target_indices:
        if exclude_idx is not None and idx == exclude_idx:
            continue
        target, case = bucket[idx]
        m = case.fit.measure(point_by_strategy[case.request["strategy"]])
        score = m.get("rank_score")
        if score is None:
            return (idx, target["pair"], m.get("reason", "нет допустимого результата")), scores
        scores.append((idx, float(score), m["utility"] is not None))
    return None, scores


def _worker_eval_plateau_batch(
    cohort_id: str,
    points: list[tuple[int, dict[str, dict[str, Any]]]],
) -> dict[int, tuple[tuple[int, str, str] | None, list[tuple[int, float, bool]]]]:
    """Evaluate all uncached plateau neighbor points across this shard's windows."""
    bucket = _WORKER_COHORTS.get(cohort_id, {})
    ordered_indices = sorted(bucket)
    out: dict[int, tuple[tuple[int, str, str] | None, list[tuple[int, float, bool]]]] = {}
    for p_idx, point_by_strategy in points:
        scores: list[tuple[int, float, bool]] = []
        failure = None
        for idx in ordered_indices:
            target, case = bucket[idx]
            m = case.fit.measure(point_by_strategy[case.request["strategy"]])
            score = m.get("rank_score")
            if score is None:
                failure = (idx, target["pair"], m.get("reason", "нет допустимого результата"))
                break
            scores.append((idx, float(score), m["utility"] is not None))
        out[p_idx] = (failure, scores)
    return out


def _worker_report_cohort(
    cohort_id: str,
    point_by_strategy: dict[str, dict[str, Any]],
    best_is_none: bool,
    settings: ResearchSettings,
) -> list[tuple[int, dict[str, Any], bool, dict[str, Any] | None]]:
    """Build final member evaluations and report payloads in parallel."""
    bucket = _WORKER_COHORTS.get(cohort_id, {})
    rows: list[tuple[int, dict[str, Any], bool, dict[str, Any] | None]] = []
    for idx in sorted(bucket):
        target, case = bucket[idx]
        local = point_by_strategy[case.request["strategy"]]
        full = case.full
        exec_ref = bool(full.get("execution_reference", False))
        selection = {
            **full["selection"],
            **{k: local[k] for k in case.execution_axes},
            "parameters": {k: local[k] for k in case.strategy_axes},
        }
        if best_is_none and any(not a.contains(local[k]) for k, a in case.axes.items()):
            rows.append(
                (
                    idx,
                    {
                        "target": target,
                        "reason": "Исходные параметры не входят в общий диапазон.",
                    },
                    exec_ref,
                    None,
                )
            )
            continue
        training_rate = case.fit.measure(local).get("rank_score")
        row = {
            "target": target,
            "selection": selection,
            "input_run_id": full["run_id"],
            "training_annualized_excess_pp": training_rate,
        }
        try:
            single = case.report(local)
            rows.append(
                (
                    idx,
                    {**row, "run_id": single["run_id"], "evaluation": single["evaluation"]},
                    exec_ref,
                    single,
                )
            )
        except (W0Error, OSError) as exc:
            rows.append(
                (
                    idx,
                    {
                        **row,
                        "evaluation": {
                            **settings.metadata(),
                            **case.split.metadata(),
                            "execution_reference": exec_ref,
                            "result": {"reason": str(exc)},
                        },
                    },
                    exec_ref,
                    None,
                )
            )
    return rows


def _await_futures(futures, check_cancelled, on_done=None):
    pending = set(futures)
    while pending:
        try:
            check_cancelled()
        except BaseException:
            for fut in pending:
                fut.cancel()
            raise
        done, pending = wait(pending, timeout=POLL_INTERVAL_SECONDS, return_when=FIRST_COMPLETED)
        if on_done is not None and done:
            for fut in done:
                on_done(fut)
    return [unwrap_worker_result(fut.result()) for fut in futures]


class CohortWorkerPool:
    """Long-lived spawn worker pool for cohort search, plateau, and window summaries."""

    def __init__(self, max_workers: int | None = None):
        self.max_workers = max_workers if max_workers is not None else configured_workers()
        self._lock = threading.Lock()
        self._executor: ProcessPoolExecutor | None = None
        self._worker_slots: list[ProcessPoolExecutor] = []
        self._closed = False
        self._atexit_ref = weakref.ref(self)
        atexit.register(_shutdown_weak, self._atexit_ref)

    def _ensure_workers(self, count: int) -> list[ProcessPoolExecutor]:
        """One-worker-per-executor lets us pin sticky cohort shards to exact processes."""
        with self._lock:
            if self._closed:
                raise RuntimeError("CohortWorkerPool is closed")
            ctx = multiprocessing.get_context("spawn")
            while len(self._worker_slots) < count:
                self._worker_slots.append(ProcessPoolExecutor(max_workers=1, mp_context=ctx))
            return list(self._worker_slots[:count])

    def should_use(self, count: int, estimated_bars: int) -> bool:
        if self.max_workers <= 1 or count < MIN_POOL_WINDOWS:
            return False
        if os.environ.get("MOEX_PROOF_FORCE_POOL") == "1":
            return True
        return estimated_bars >= MIN_POOL_BARS or count >= 4

    def init_cohort(
        self,
        root: Path,
        settings: ResearchSettings,
        benchmark: tuple[list[str], dict[str, float]],
        items: list[tuple[int, dict[str, Any], dict[str, Any], bool]],
        check_cancelled,
        on_progress=None,
    ) -> dict[str, Any]:
        """Distribute windows LPT-wise across worker processes and initialize StudyWindows."""
        n = len(items)
        w = min(self.max_workers, n)
        slots = self._ensure_workers(w)
        seal_cache: dict[str, Any] = {}
        weights = {
            entry[0]: _estimate_weight(root, entry[1], settings.timeframe, _seal_cache=seal_cache)
            for entry in items
        }
        weighted = sorted(items, key=lambda entry: weights[entry[0]], reverse=True)
        shards: list[list[tuple[int, dict[str, Any], dict[str, Any], bool]]] = [
            [] for _ in range(w)
        ]
        shard_loads = [0] * w
        owner_of: dict[int, int] = {}
        for entry in weighted:
            s_idx = min(range(w), key=lambda k: shard_loads[k])
            shards[s_idx].append(entry)
            shard_loads[s_idx] += weights[entry[0]]
            owner_of[entry[0]] = s_idx
        for shard in shards:
            shard.sort(key=lambda entry: entry[0])

        cohort_id = uuid.uuid4().hex
        root_str = str(root.resolve())
        futures = []
        fut_weights: dict[Any, int] = {}
        for s_idx, shard in enumerate(shards):
            if shard:
                fut = submit_worker(
                    slots[s_idx],
                    _worker_init_cohort,
                    cohort_id,
                    root_str,
                    settings,
                    benchmark,
                    shard,
                )
                futures.append(fut)
                fut_weights[fut] = len(shard)
        done_count = 0

        def _on_done(fut) -> None:
            nonlocal done_count
            done_count += fut_weights.get(fut, 0)
            if on_progress is not None:
                on_progress(done_count, n)

        try:
            chunks = _await_futures(
                futures,
                check_cancelled,
                on_done=_on_done if on_progress is not None else None,
            )
        except BaseException:
            self.release_cohort(cohort_id, owner_of)
            raise
        combined: list[tuple[int, str, dict[str, Any]]] = []
        for chunk in chunks:
            combined.extend(chunk)
        combined.sort(key=lambda item: item[0])
        return {
            "cohort_id": cohort_id,
            "slots": slots,
            "shards": shards,
            "owner_of": owner_of,
            "results": combined,
        }

    def release_cohort(self, cohort_id: str, owner_of: dict[int, int] | None = None) -> None:
        with self._lock:
            slots = list(self._worker_slots)
        if not slots:
            return
        indices = (
            sorted(set(owner_of.values())) if owner_of is not None else list(range(len(slots)))
        )
        for s_idx in indices:
            if s_idx < len(slots):
                try:
                    slots[s_idx].submit(_worker_release_cohort, cohort_id)
                except RuntimeError:
                    pass

    def eval_point(
        self,
        cohort_state: dict[str, Any],
        point_by_strategy: dict[str, dict[str, Any]],
        canary_box: list[int],
        total_members: int,
        check_cancelled,
    ) -> dict[str, Any]:
        """Adaptive Canary / Single-Roundtrip Fan-Out with guaranteed canonical minimum-index failure reason."""
        cohort_id = cohort_state["cohort_id"]
        slots = cohort_state["slots"]
        owner_of = cohort_state["owner_of"]
        shards = cohort_state["shards"]
        use_canary = bool(cohort_state.get("canary_active"))

        if not use_canary:
            # Optimistic single-roundtrip fan-out across all active shards simultaneously.
            active_slots = sorted(set(owner_of.values()))
            futs = [
                submit_worker(
                    slots[s_idx], _worker_eval_indices, cohort_id, point_by_strategy, None, None
                )
                for s_idx in active_slots
            ]
            all_scores: list[tuple[int, float, bool]] = []
            min_fail = None
            for sub_fail, sub_scores in _await_futures(futs, check_cancelled):
                if sub_fail is not None:
                    if min_fail is None or sub_fail[0] < min_fail[0]:
                        min_fail = sub_fail
                all_scores.extend(sub_scores)
            if min_fail is not None:
                canary_box[0] = min_fail[0]
                cohort_state["canary_active"] = True
                return {
                    "utility": None,
                    "rank_score": None,
                    "reason": f"{min_fail[1]}: {min_fail[2]}",
                }
            all_scores.sort(key=lambda item: item[0])
            ordered_scores = [item[1] for item in all_scores]
            exposed = any(item[2] for item in all_scores)
            score = fmean(ordered_scores)
            return {
                "rank_score": score,
                "by_window_pp": ordered_scores,
                "utility": score if exposed else None,
                **({} if exposed else {"reason": "Открытого спреда не было на обучении."}),
            }

        canary = canary_box[0]

        # Phase 1: probe the canary window first (1 IPC round-trip).
        canary_slot = slots[owner_of[canary]]
        fail, canary_scores = _await_futures(
            [
                submit_worker(
                    canary_slot, _worker_eval_indices, cohort_id, point_by_strategy, [canary]
                )
            ],
            check_cancelled,
        )[0]
        if fail is not None:
            if canary == 0:
                return {
                    "utility": None,
                    "rank_score": None,
                    "reason": f"{fail[1]}: {fail[2]}",
                }
            # A lower canonical index may also fail on this point; inspect only < canary.
            earlier_slots = sorted({owner_of[i] for i in range(canary)})
            sub_futs = [
                submit_worker(
                    slots[s_idx],
                    _worker_eval_indices,
                    cohort_id,
                    point_by_strategy,
                    [entry[0] for entry in shards[s_idx] if entry[0] < canary],
                )
                for s_idx in earlier_slots
            ]
            best_fail = fail
            for sub_fail, _ in _await_futures(sub_futs, check_cancelled):
                if sub_fail is not None and sub_fail[0] < best_fail[0]:
                    best_fail = sub_fail
            canary_box[0] = best_fail[0]
            return {
                "utility": None,
                "rank_score": None,
                "reason": f"{best_fail[1]}: {best_fail[2]}",
            }

        # Canary passed on this point; switch back to single-roundtrip fan-out for subsequent passing trials.
        cohort_state["canary_active"] = False

        # Phase 2: canary passed; evaluate all remaining windows across active shards.
        if total_members == 1:
            _idx, score, exp = canary_scores[0]
            return {
                "rank_score": score,
                "by_window_pp": [score],
                "utility": score if exp else None,
                **({} if exp else {"reason": "Открытого спреда не было на обучении."}),
            }

        active_slots = sorted(set(owner_of.values()))
        futs = [
            submit_worker(
                slots[s_idx], _worker_eval_indices, cohort_id, point_by_strategy, None, canary
            )
            for s_idx in active_slots
            if any(entry[0] != canary for entry in shards[s_idx])
        ]
        all_scores = list(canary_scores)
        min_fail = None
        for sub_fail, sub_scores in _await_futures(futs, check_cancelled):
            if sub_fail is not None:
                if min_fail is None or sub_fail[0] < min_fail[0]:
                    min_fail = sub_fail
            all_scores.extend(sub_scores)
        if min_fail is not None:
            canary_box[0] = min_fail[0]
            cohort_state["canary_active"] = True
            return {
                "utility": None,
                "rank_score": None,
                "reason": f"{min_fail[1]}: {min_fail[2]}",
            }
        all_scores.sort(key=lambda item: item[0])
        ordered_scores = [item[1] for item in all_scores]
        exposed = any(item[2] for item in all_scores)
        score = fmean(ordered_scores)
        return {
            "rank_score": score,
            "by_window_pp": ordered_scores,
            "utility": score if exposed else None,
            **({} if exposed else {"reason": "Открытого спреда не было на обучении."}),
        }

    def eval_plateau_batch(
        self,
        cohort_state: dict[str, Any],
        points: list[tuple[int, dict[str, dict[str, Any]]]],
        check_cancelled,
        on_progress=None,
    ) -> dict[int, dict[str, Any]]:
        """Evaluate multiple uncached neighbor points in a single parallel batch."""
        if not points:
            return {}
        cohort_id = cohort_state["cohort_id"]
        slots = cohort_state["slots"]
        active_slots = sorted(set(cohort_state["owner_of"].values()))
        futs = [
            submit_worker(slots[s_idx], _worker_eval_plateau_batch, cohort_id, points)
            for s_idx in active_slots
        ]
        total_slots = len(futs)
        done_slots = 0

        def _on_done(_fut) -> None:
            nonlocal done_slots
            done_slots += 1
            if on_progress is not None:
                on_progress(done_slots, total_slots)

        shard_maps = _await_futures(
            futs, check_cancelled, on_done=_on_done if on_progress is not None else None
        )
        results: dict[int, dict[str, Any]] = {}
        for p_idx, _ in points:
            min_fail = None
            all_scores: list[tuple[int, float, bool]] = []
            for smap in shard_maps:
                sub_fail, sub_scores = smap[p_idx]
                if sub_fail is not None and (min_fail is None or sub_fail[0] < min_fail[0]):
                    min_fail = sub_fail
                all_scores.extend(sub_scores)
            if min_fail is not None:
                results[p_idx] = {
                    "utility": None,
                    "rank_score": None,
                    "reason": f"{min_fail[1]}: {min_fail[2]}",
                }
            else:
                all_scores.sort(key=lambda item: item[0])
                ordered_scores = [item[1] for item in all_scores]
                exposed = any(item[2] for item in all_scores)
                score = fmean(ordered_scores)
                results[p_idx] = {
                    "rank_score": score,
                    "by_window_pp": ordered_scores,
                    "utility": score if exposed else None,
                    **({} if exposed else {"reason": "Открытого спреда не было на обучении."}),
                }
        return results

    def report_cohort(
        self,
        cohort_state: dict[str, Any],
        point_by_strategy: dict[str, dict[str, Any]],
        best_is_none: bool,
        settings: ResearchSettings,
        check_cancelled,
        on_progress=None,
    ) -> list[tuple[int, dict[str, Any], bool, dict[str, Any] | None]]:
        cohort_id = cohort_state["cohort_id"]
        slots = cohort_state["slots"]
        shards = cohort_state["shards"]
        active_slots = sorted(set(cohort_state["owner_of"].values()))
        total_members = sum(len(shards[s_idx]) for s_idx in active_slots)
        futs = []
        fut_weights: dict[Any, int] = {}
        for s_idx in active_slots:
            fut = submit_worker(
                slots[s_idx],
                _worker_report_cohort,
                cohort_id,
                point_by_strategy,
                best_is_none,
                settings,
            )
            futs.append(fut)
            fut_weights[fut] = len(shards[s_idx])
        done_count = 0

        def _on_done(fut) -> None:
            nonlocal done_count
            done_count += fut_weights.get(fut, 0)
            if on_progress is not None:
                on_progress(done_count, total_members)

        combined: list[tuple[int, dict[str, Any], bool, dict[str, Any] | None]] = []
        for chunk in _await_futures(
            futs, check_cancelled, on_done=_on_done if on_progress is not None else None
        ):
            combined.extend(chunk)
        combined.sort(key=lambda item: item[0])
        return combined

    def window_summaries(
        self,
        root: Path,
        settings: ResearchSettings,
        benchmark: tuple[list[str], dict[str, float]],
        items: list[tuple[int, dict[str, Any], dict[str, Any]]],
        check_cancelled,
        on_progress=None,
    ) -> list[dict[str, Any]]:
        n = len(items)
        w = min(self.max_workers, n)
        slots = self._ensure_workers(w)
        seal_cache: dict[str, Any] = {}
        weights = {
            entry[0]: _estimate_weight(root, entry[1], settings.timeframe, _seal_cache=seal_cache)
            for entry in items
        }
        weighted = sorted(items, key=lambda entry: weights[entry[0]], reverse=True)
        # Split into up to 2 micro-batches per worker for larger sets so progress updates smoothly.
        batches = min(n, w * 2) if n >= w * 2 else w
        shards: list[list[tuple[int, dict[str, Any], dict[str, Any]]]] = [
            [] for _ in range(batches)
        ]
        shard_loads = [0] * batches
        for entry in weighted:
            s_idx = min(range(batches), key=lambda k: shard_loads[k])
            shards[s_idx].append(entry)
            shard_loads[s_idx] += weights[entry[0]]
        root_str = str(root.resolve())
        futs = []
        fut_weights: dict[Any, int] = {}
        for b_idx, shard in enumerate(shards):
            if shard:
                fut = submit_worker(
                    slots[b_idx % w], _worker_window_summaries, root_str, settings, benchmark, shard
                )
                futs.append(fut)
                fut_weights[fut] = len(shard)
        done_count = 0

        def _on_done(fut) -> None:
            nonlocal done_count
            done_count += fut_weights.get(fut, 0)
            if on_progress is not None:
                on_progress(done_count, n)

        combined: list[tuple[int, dict[str, Any]]] = []
        for chunk in _await_futures(
            futs, check_cancelled, on_done=_on_done if on_progress is not None else None
        ):
            combined.extend(chunk)
        combined.sort(key=lambda entry: entry[0])
        return [entry[1] for entry in combined]

    def shutdown(self, wait_for: bool = True) -> None:
        with self._lock:
            self._closed = True
            slots = list(self._worker_slots)
            self._worker_slots.clear()
        for slot in slots:
            try:
                slot.shutdown(wait=wait_for, cancel_futures=True)
            except Exception:
                pass


def _shutdown_weak(ref: weakref.ref[CohortWorkerPool]) -> None:
    pool = ref()
    if pool is not None:
        pool.shutdown(wait_for=False)


def get_worker_pool(service: Any) -> CohortWorkerPool:
    if service._pool is None:
        service._pool = CohortWorkerPool()
    return service._pool
