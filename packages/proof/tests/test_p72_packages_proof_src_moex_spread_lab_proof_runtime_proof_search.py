"""Regression coverage for strict typing of ``proof_runtime.proof_search`` (Issue #92).

The module is a re-export facade over the kernel, evaluation, validation and search
modules. Strict typing holds only while the facade keeps pointing at the same objects,
keeps the progress contract of :func:`research_run` (stage order, percentages), and
hands the plateau diagnostics the training landscape of the study window.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime import (
    evaluation,
    proof_kernel,
    proof_search,
    request_validation,
    service,
)
from moex_spread_lab.proof_runtime.proof_search import research_run


def _fake_window(events: list[str]) -> Any:
    class _Window:
        def __init__(self) -> None:
            self.seed = {"lookback": 10, "entry_z": 1.0, "exit_z": 0.5}
            self.fit = object()

        def report(self, point: dict[str, Any]) -> dict[str, Any]:
            events.append("report")
            return {"window": {"point": dict(point)}}

        def admission(self) -> str | None:
            events.append("admission")
            return None

        def lamp_warning(self) -> str | None:
            events.append("lamp_warning")
            return None

    return _Window()


class ProofSearchStrictFacadeTests(unittest.TestCase):
    def test_facade_reexports_are_the_same_objects_as_their_owners(self):
        self.assertIs(proof_search._lamps, proof_kernel._lamps)
        self.assertIs(proof_search._ordered, proof_kernel._ordered)
        self.assertIs(proof_search._measure, proof_kernel._measure)
        self.assertIs(proof_search._run_id, proof_kernel._run_id)
        self.assertIs(proof_search.StudyWindow, proof_kernel.StudyWindow)
        self.assertIs(proof_search._excess_curve, evaluation._excess_curve)
        self.assertIs(proof_search.account_checks, evaluation.account_checks)
        self.assertIs(proof_search._validate, request_validation._validate)
        self.assertIs(proof_search._identity, request_validation._identity)
        self.assertIs(proof_search.execution_axes, request_validation.execution_axes)
        self.assertIs(proof_search.compact_result, request_validation.compact_result)
        self.assertIs(proof_search._benchmark_marks, service._benchmark_marks)
        self.assertIs(proof_search._mark, service._mark)
        self.assertIs(proof_search.ProofService, service.ProofService)
        self.assertIs(proof_search.reference_eval, service.reference_eval)

    def test_deprecated_axes_alias_and_unknown_attribute(self):
        self.assertEqual(proof_search.STRATEGY_AXES, proof_search.get_strategy_axes())
        with self.assertRaises(AttributeError):
            _ = proof_search.PROOF_ADAPTER_IDS

    def test_signature_keeps_keyword_only_flags_and_safe_defaults(self):
        parameters = inspect.signature(research_run).parameters
        self.assertEqual(list(parameters)[:2], ["service", "request"])
        self.assertEqual(parameters["optimize"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(parameters["settings_override"].default, None)
        self.assertIs(parameters["include_plateau"].default, True)
        self.assertIs(parameters["on_progress"].default, None)
        # The default cancellation probe must be callable and silent.
        self.assertIsNone(parameters["check_cancelled"].default())

    def test_progress_stages_and_plateau_receive_the_fit_landscape(self):
        events: list[str] = []
        window = _fake_window(events)
        seen: list[tuple[Any, Any]] = []

        def fake_lamps(land: Any, point: Any, reason: Any = None, *, on_progress: Any = None):
            seen.append((land, reason))
            return {"fields": {}, "basis": "training"}

        progress: list[dict[str, Any]] = []
        with (
            patch.object(proof_search, "_validate"),
            patch.object(proof_search, "ResearchSettings", _Settings()),
            patch.object(proof_search, "_benchmark_marks", lambda path: ([], {})),
            patch.object(proof_search, "StudyWindow", lambda *args: window),
            patch.object(proof_search, "_lamps", fake_lamps),
        ):
            report = research_run(_FakeService(), {}, optimize=False, on_progress=progress.append)
        self.assertEqual(
            [(item["stage"], item["done"], item["total"], item["percent"]) for item in progress],
            [("prepare", 0, 1, 15.0), ("run", 1, 2, 35.0), ("plateau", 0, 1, 58.0)],
        )
        self.assertEqual(events, ["report", "admission", "lamp_warning"])
        self.assertIs(seen[0][0], window.fit)
        self.assertIsNone(seen[0][1])
        self.assertEqual(report["plateau"], {"fields": {}, "basis": "training"})
        self.assertEqual(set(report), {"window", "plateau"})

    def test_plateau_progress_percentages_stay_bounded(self):
        window = _fake_window([])
        progress: list[dict[str, Any]] = []

        def fake_lamps(land: Any, point: Any, reason: Any = None, *, on_progress: Any = None):
            on_progress(1, 2)
            on_progress(2, 2)
            on_progress(0, 0)
            return {}

        with (
            patch.object(proof_search, "_validate"),
            patch.object(proof_search, "ResearchSettings", _Settings()),
            patch.object(proof_search, "_benchmark_marks", lambda path: ([], {})),
            patch.object(proof_search, "StudyWindow", lambda *args: window),
            patch.object(proof_search, "_lamps", fake_lamps),
        ):
            research_run(_FakeService(), {}, optimize=False, on_progress=progress.append)
        reported = [item["percent"] for item in progress if item["stage"] == "plateau"]
        self.assertEqual(reported, [58.0, 76.5, 95.0, 58.0])

    def test_plateau_can_be_skipped_without_touching_admission(self):
        events: list[str] = []
        window = _fake_window(events)
        progress: list[dict[str, Any]] = []
        with (
            patch.object(proof_search, "_validate"),
            patch.object(proof_search, "ResearchSettings", _Settings()),
            patch.object(proof_search, "_benchmark_marks", lambda path: ([], {})),
            patch.object(proof_search, "StudyWindow", lambda *args: window),
        ):
            report = research_run(
                _FakeService(),
                {},
                optimize=False,
                include_plateau=False,
                on_progress=progress.append,
            )
        self.assertEqual(events, ["report"])
        self.assertNotIn("plateau", report)
        self.assertEqual([item["percent"] for item in progress], [15.0, 60.0])
        self.assertEqual([item["total"] for item in progress], [1, 1])

    def test_search_dispatch_and_journal_only_rejections(self):
        calls: list[str] = []
        with (
            patch.object(proof_search, "_validate"),
            patch.object(proof_search, "ResearchSettings", _Settings()),
            patch.object(proof_search, "_benchmark_marks", lambda path: ([], {})),
            patch.object(
                proof_search,
                "_batch_search",
                lambda *args, **kwargs: calls.append("batch") or {"status": "batch"},
            ),
            patch.object(
                proof_search,
                "_strategy_search",
                lambda *args, **kwargs: calls.append("strategy") or {"status": "strategy"},
            ),
        ):
            svc = _FakeService()
            self.assertEqual(research_run(svc, {}, optimize=True), {"status": "batch"})
            self.assertEqual(
                research_run(svc, {"optimize_strategy": True}, optimize=True),
                {"status": "strategy"},
            )
            with self.assertRaisesRegex(W0Error, "подбора"):
                research_run(svc, {"optimize_strategy": True}, optimize=False)
            with self.assertRaises(W0Error):
                research_run(svc, {"targets": [{"pair": "x"}]}, optimize=False)
        self.assertEqual(calls, ["batch", "strategy"])

    def test_measure_provider_swap_is_restored_after_the_test(self):
        provider = lambda: proof_kernel._measure
        previous = proof_kernel._measure_provider
        try:
            proof_search._set_measure_provider(provider)
            self.assertIs(proof_kernel._measure_provider, provider)
        finally:
            proof_search._set_measure_provider(previous)
        self.assertIs(proof_kernel._measure_provider, previous)

    def test_reference_eval_matches_the_service_facade(self):
        self.assertEqual(proof_search.reference_eval(), service.reference_eval())


class _Settings:
    """Stand-in for ``ResearchSettings``: the facade only calls ``load``."""

    @staticmethod
    def load(root: Path) -> object:
        return object()


class _FakeService:
    root = Path(tempfile.gettempdir())


if __name__ == "__main__":
    unittest.main()
