"""Pre-P7.2 export-surface regressions for the proof cohort preparation and workers.

Issue #137 audit result: the P7.2 strict-typing passes (#80, #99, #100) added an
``__all__`` to each of these three modules in order to silence
``reportUnusedFunction`` for hooks that other modules import by name. That silently
narrowed every ``from ... import *`` surface to the hook list and dropped the
imported bindings star imports used to expose.

The supported contract is *both* halves at once, so this module pins:

* the wildcard surface of each module against its own immediate pre-leaf revision
  (``cohort_prepare_helpers`` @ ``ae45065``, ``worker_cohort`` @ ``735c496``,
  ``worker_window`` @ ``fa9f4db``), recorded here as the exact name sets a star
  import produced at those revisions;
* the historical private names those hooks are still reached through, with the
  original object identity preserved across the facade chain and late binding
  intact after a monkeypatch.

Behaviour is untouched: the payload/selection/run_id expectations stay in the
leaf-owned suites, and nothing here re-derives business logic.
"""

from __future__ import annotations

from typing import Any

import pytest

from moex_spread_lab.proof_runtime import (
    cohort_models,
    cohort_prepare,
    cohort_prepare_helpers,
    proof_batch,
    proof_kernel,
    proof_pool,
    request_validation,
    worker_cohort,
    worker_window,
)
from moex_spread_lab.proof_runtime import (
    pool_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_pool as pool_tasks,
)

# Wildcard surfaces measured by running ``from <module> import *`` at each module's own
# immediate pre-leaf revision. These are the bindings strict P7.2 must not drop.
PRESERVED_WILDCARDS: dict[str, set[str]] = {
    "cohort_prepare_helpers": {
        "Axis",
        "CALENDAR_UNIVERSE",
        "InstrumentContext",
        "MAX_TARGETS",
        "W0Error",
        "annotations",
        "execution_axes",
        "get_strategy_axes",
        "resolve_strategy_registry",
    },
    "worker_cohort": {
        "Any",
        "CALENDAR_UNIVERSE",
        "CalendarData",
        "CarryStrategyError",
        "ResearchSettings",
        "StudyWindow",
        "W0Error",
        "annotations",
        "cast",
        "get_strategy_axes",
    },
    "worker_window": {
        "Any",
        "Path",
        "ResearchSettings",
        "StudyWindow",
        "W0Error",
        "compact_result",
    },
}

_HELPER_HOOKS = (
    "_active_seed",
    "_confirmation",
    "_filter_training",
    "_intersect_axes",
    "_native_request",
    "_targets",
    "_translate",
)
_COHORT_TASKS = (
    "_WORKER_COHORTS",
    "_prepare_worker_case",
    "_worker_eval_indices",
    "_worker_eval_plateau_batch",
    "_worker_init_cohort",
    "_worker_release_cohort",
    "_worker_report_cohort",
)


def _star_names(module_name: str) -> set[str]:
    namespace: dict[str, Any] = {}
    exec(f"from moex_spread_lab.proof_runtime.{module_name} import *", namespace)
    return set(namespace) - {"__builtins__"}


@pytest.mark.parametrize(
    ("module_name", "expected"),
    list(PRESERVED_WILDCARDS.items()),
)
def test_p72_exports_proof_workers_wildcard_matches_pre_leaf_surface(
    module_name: str, expected: set[str]
) -> None:
    assert _star_names(module_name) == expected


@pytest.mark.parametrize(
    ("module_name", "expected"),
    list(PRESERVED_WILDCARDS.items()),
)
def test_p72_exports_proof_workers_all_declares_the_wildcard_surface(
    module_name: str, expected: set[str]
) -> None:
    # ``__all__`` is the declared form of the measured surface, not a second, narrower one.
    module = {
        "cohort_prepare_helpers": cohort_prepare_helpers,
        "worker_cohort": worker_cohort,
        "worker_window": worker_window,
    }[module_name]
    assert set(module.__all__) == expected


def test_p72_exports_proof_workers_helper_hooks_keep_names_and_identity() -> None:
    # Every hook the facades resolve must still exist under its historical name.
    for name in _HELPER_HOOKS:
        assert callable(getattr(cohort_prepare_helpers, name))
        assert getattr(cohort_prepare_helpers, name[1:]) is getattr(cohort_prepare_helpers, name)


def test_p72_exports_proof_workers_helper_hooks_stay_reachable_through_facades() -> None:
    assert cohort_prepare_helpers._identity is request_validation._identity
    assert cohort_prepare_helpers.execution_axes is request_validation.execution_axes
    for name in _HELPER_HOOKS:
        hook = getattr(cohort_prepare_helpers, name)
        assert getattr(proof_batch, name) is hook
        assert getattr(cohort_prepare, name) is hook
    # No other module imports these hooks directly; the facades above are the chain.
    assert "_cohort_prepare_helpers" not in vars(cohort_models)


def test_p72_exports_proof_workers_helper_hooks_stay_late_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reading through the module dict, as ``proof_batch``/``cohort_prepare`` do, must see a
    # patch installed after import: the binding is a live attribute, not a copied snapshot.
    sentinel = object()
    monkeypatch.setattr(cohort_prepare_helpers, "_targets", sentinel)
    assert cohort_prepare_helpers.__dict__["_targets"] is sentinel
    assert proof_batch._ch["_targets"] is sentinel
    assert cohort_prepare._ch["_targets"] is sentinel

    # ``execution_axes`` is bound straight off ``request_validation`` by the two consumers,
    # so it is only patchable there; this module must not snapshot it into a private name.
    assert cohort_prepare_helpers.execution_axes is request_validation.execution_axes
    assert cohort_prepare.execution_axes is request_validation.execution_axes


def test_p72_exports_proof_workers_cohort_tasks_stay_shared_with_pool_facade() -> None:
    for name in _COHORT_TASKS:
        assert getattr(worker_cohort, name[1:]) is getattr(worker_cohort, name)
    # ``_prepare_worker_case`` is worker-internal; the pool shares the bucket and task hooks.
    for name in ("_WORKER_COHORTS", *_COHORT_TASKS[2:]):
        hook = getattr(worker_cohort, name)
        assert getattr(proof_pool, name) is hook
    assert not hasattr(proof_pool, "_prepare_worker_case")
    task_names = {
        "_WORKER_COHORTS": "WORKER_COHORTS",
        "_worker_init_cohort": "init_cohort_task",
        "_worker_release_cohort": "release_cohort_task",
        "_worker_eval_indices": "eval_indices_task",
        "_worker_eval_plateau_batch": "eval_plateau_task",
        "_worker_report_cohort": "report_cohort_task",
    }
    for name, task_attr in task_names.items():
        assert getattr(pool_tasks, task_attr) is getattr(worker_cohort, name)


def test_p72_exports_proof_workers_window_tasks_stay_shared_with_pool_facade() -> None:
    assert worker_window.worker_service is worker_window._worker_service
    assert worker_window.worker_window_summaries is worker_window._worker_window_summaries
    assert pool_tasks.worker_service is worker_window._worker_service
    assert pool_tasks.window_summaries_task is worker_window._worker_window_summaries
    assert proof_pool._worker_service is worker_window._worker_service
    assert proof_pool._worker_window_summaries is worker_window._worker_window_summaries


def test_p72_exports_proof_workers_kernel_hooks_keep_one_object() -> None:
    # ``worker_window`` and ``worker_cohort`` bind ``StudyWindow``; the window module also
    # re-reads the kernel validation/compaction hooks at import time.
    assert worker_window.StudyWindow is proof_kernel.StudyWindow
    assert worker_cohort.StudyWindow is proof_kernel.StudyWindow
    assert worker_window.compact_result is proof_kernel.compact_result
    assert worker_window._validate is proof_kernel._validate


def test_p72_exports_proof_workers_studywindow_patch_stays_late_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both worker modules construct windows through the module attribute, so a host that
    # installs a fake there keeps control of window construction inside the worker task.
    sentinel = object()
    monkeypatch.setattr(worker_cohort, "StudyWindow", sentinel)
    assert worker_cohort.StudyWindow is sentinel
    assert worker_window.StudyWindow is not sentinel


def test_p72_exports_proof_workers_window_summaries_patch_point_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``pool_reports`` resolves the summaries task off the ``proof_pool`` facade.
    def fake_summaries(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(proof_pool, "_worker_window_summaries", fake_summaries)
    assert proof_pool._worker_window_summaries is fake_summaries
    assert worker_window._worker_window_summaries is not fake_summaries
