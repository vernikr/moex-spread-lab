"""Regression coverage for P7.2 strict typing of proof_runtime/proof_windows.py (#94)."""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from moex_spread_lab.core.settings import ResearchSettings, save_settings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof import proof_kernel, proof_windows, request_validation
from moex_spread_lab.proof.payload_tools import benchmark_marks
from moex_spread_lab.proof.worker_weight import estimate_weight

CAL = {
    "universe": "calendar_native",
    "asset": "SR",
    "pair": "SRH3/SRM3",
    "strategy": "zscore_mean_reversion",
    "parameters": {"window": 10, "entry_z": 1.5, "exit_z": 0.5},
    "sizing": "max_affordable",
}
PAIR = {
    "universe": "pair_screen",
    "pair": "CNYRUBF/CRZ6",
    "strategy": "mean_reversion",
    "parameters": {"lookback": 6, "entry_z": 0.8, "exit_z": 0.1},
    "sizing": "max_affordable",
}
CAL_TARGET = {"universe": "calendar_native", "pair": "SRH3/SRM3", "asset": "SR"}
PAIR_TARGET = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6"}
BENCHMARK = (["2024-01-01"], {})


def _stub_report() -> dict:
    return {
        "summary": {"trades": 2, "closed_trades": 1, "open_positions": 1},
        "selection": {"pair": "X"},
        "run_id": "run-stub-1",
        "evaluation": {"result": {"annualized_excess_pp": 1.5}},
        "window": {"near": "SRH3"},
    }


class _StubWindow:
    """Minimal StudyWindow surface: one fixed seed point and one canned report."""

    def __init__(self, service, item, settings, benchmark, calendar=None):
        self.item = item
        self.seed = {"entry_z": 0.5}

    def report(self, point):
        return _stub_report()


class _RecordingPool:
    def __init__(self, use: bool = True):
        self.use = use
        self.why: tuple[int, int] | None = None
        self.dispatched: Any = None

    def should_use(self, count, estimated_bars):
        self.why = (count, estimated_bars)
        return self.use

    def window_summaries(self, root, settings, benchmark, items, check_cancelled, on_progress=None):
        self.dispatched = (root, settings, benchmark, items, check_cancelled)
        if on_progress is not None:
            on_progress(1, len(items))
            on_progress(len(items), len(items))
        return [{"status": "measured", "pool_row": idx} for idx, _item, _target in items]


def _service(root: Path, calendar: object | None) -> SimpleNamespace:
    return SimpleNamespace(root=root, _calendar=calendar, _pool=None)


def _assert_w0_error(body: object) -> None:
    try:
        proof_windows._window_identities(body)
    except W0Error:
        return
    raise AssertionError(f"expected W0Error for {body!r}")


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_windows_bindings() -> None:
    assert proof_windows._identity is request_validation._identity
    assert proof_windows._validate is request_validation._validate
    assert proof_windows.compact_result is request_validation.compact_result
    assert proof_windows._estimate_weight is estimate_weight
    assert proof_windows._benchmark_marks is benchmark_marks
    assert proof_windows.StudyWindow is proof_kernel.StudyWindow
    assert proof_windows._DEFAULT_STUDY_WINDOW is proof_windows.StudyWindow
    assert proof_windows.MAX_WINDOW_REQUESTS == 128


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_windows_identities() -> None:
    request = {"requests": [CAL, PAIR]}
    original = copy.deepcopy(request)
    assert proof_windows._window_identities(request) == [CAL_TARGET, PAIR_TARGET]
    assert request == original
    for body in (
        None,
        [],
        "windows",
        {},
        {"requests": [], "targets": []},
        {"requests": []},
        {"requests": [CAL] * (proof_windows.MAX_WINDOW_REQUESTS + 1)},
        {"requests": [CAL, dict(CAL)]},
        {"requests": [{"targets": []}]},
        {"requests": [123]},
        {"requests": [{"pair": None}]},
    ):
        _assert_w0_error(body)


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_windows_settings_progress() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert proof_windows._settings(root, None) == ResearchSettings.load(root)
        override = ResearchSettings(slippage_ticks=2.5)
        assert proof_windows._settings(root, override) is override
        save_settings(root, {"validation_percent": 25})
        assert proof_windows._settings(root, None).validation_percent == 25
    events = []
    proof_windows._progress(events.append, 0, 4)
    proof_windows._progress(events.append, 2, 4)
    proof_windows._progress(events.append, 4, 4)
    proof_windows._progress(None, 1, 0)
    assert events == [
        {"stage": "windows", "done": 0, "total": 4, "percent": 4.0, "label": "Окна: 0/4"},
        {"stage": "windows", "done": 2, "total": 4, "percent": 51.0, "label": "Окна: 2/4"},
        {"stage": "windows", "done": 4, "total": 4, "percent": 98.0, "label": "Окна: 4/4"},
    ]


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_windows_sequential_rows() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        events = []
        with (
            patch.object(proof_windows, "StudyWindow", _StubWindow),
            patch.object(proof_windows, "_benchmark_marks", lambda path: BENCHMARK),
        ):
            result = proof_windows.window_summaries(
                _service(root, object()),
                {"requests": [CAL, PAIR]},
                on_progress=events.append,
            )
        rows = result["rows"]
        assert [row["target"] for row in rows] == [CAL_TARGET, PAIR_TARGET]
        for row in rows:
            assert row["status"] == "measured"
            assert row["run_id"] == "run-stub-1"
            assert row["trades"] == 2
            assert row["annualized_excess_pp"] == 1.5
            assert row["evaluation"] == {"result": {"annualized_excess_pp": 1.5}}
            assert "ledger" not in row
        assert result["slippage_ticks"] == 1.0
        assert [event["done"] for event in events] == [0, 1, 2]
        assert [event["percent"] for event in events] == [4.0, 51.0, 98.0]


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_windows_unavailable_rows() -> (
    None
):
    def window_factory(service, item, settings, benchmark, calendar=None):
        if item.get("pair") == PAIR["pair"]:
            raise OSError("нет данных")
        return _StubWindow(service, item, settings, benchmark, calendar)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with (
            patch.object(proof_windows, "StudyWindow", window_factory),
            patch.object(proof_windows, "_benchmark_marks", lambda path: BENCHMARK),
        ):
            result = proof_windows.window_summaries(
                _service(root, object()), {"requests": [CAL, PAIR]}
            )
        measured, unavailable = result["rows"]
        assert measured["status"] == "measured"
        assert unavailable["target"] == PAIR_TARGET
        assert unavailable["status"] == "unavailable"
        assert unavailable["trades"] is None
        assert unavailable["annualized_excess_pp"] is None
        assert unavailable["reason"] == "нет данных"


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_windows_pool_dispatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pool = _RecordingPool(use=True)
        weights = []

        def estimate(root_arg, item, timeframe):
            weights.append((root_arg, item, timeframe))
            return 7

        def quiet() -> None:
            """check_cancelled stand-in with a distinct identity for forwarding asserts."""

        events = []
        with (
            patch.object(proof_windows, "get_worker_pool", lambda svc: pool),
            patch.object(proof_windows, "_estimate_weight", estimate),
            patch.object(proof_windows, "_benchmark_marks", lambda path: BENCHMARK),
        ):
            result = proof_windows.window_summaries(
                _service(root, None),
                {"requests": [CAL, PAIR]},
                check_cancelled=quiet,
                on_progress=events.append,
            )
        assert pool.why == (2, 14)
        _root, settings, benchmark, items, check_cancelled = pool.dispatched
        assert settings == ResearchSettings.load(root)
        assert benchmark == BENCHMARK
        assert items == [(0, CAL, CAL_TARGET), (1, PAIR, PAIR_TARGET)]
        assert check_cancelled is quiet
        assert [entry[1] for entry in weights] == [CAL_TARGET, PAIR_TARGET]
        assert result["rows"] == [
            {"status": "measured", "pool_row": 0},
            {"status": "measured", "pool_row": 1},
        ]
        assert [event["done"] for event in events] == [0, 1, 2]


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_windows_pool_gate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        def forbidden_pool(_svc):
            raise AssertionError("a patched StudyWindow must keep the sequential path")

        with (
            patch.object(proof_windows, "StudyWindow", _StubWindow),
            patch.object(proof_windows, "get_worker_pool", forbidden_pool),
            patch.object(proof_windows, "_benchmark_marks", lambda path: BENCHMARK),
        ):
            result = proof_windows.window_summaries(_service(root, None), {"requests": [CAL]})
        assert result["rows"][0]["status"] == "measured"

        pool = _RecordingPool(use=False)
        with (
            patch.object(proof_windows, "StudyWindow", _StubWindow),
            patch.object(proof_windows, "_DEFAULT_STUDY_WINDOW", _StubWindow),
            patch.object(proof_windows, "get_worker_pool", lambda svc: pool),
            patch.object(proof_windows, "_benchmark_marks", lambda path: BENCHMARK),
        ):
            result = proof_windows.window_summaries(_service(root, None), {"requests": [CAL]})
        assert pool.why == (1, 200)
        assert pool.dispatched is None
        assert result["rows"][0]["status"] == "measured"

        def cancel() -> None:
            raise ConnectionAbortedError()

        with (
            patch.object(proof_windows, "StudyWindow", _StubWindow),
            patch.object(proof_windows, "_benchmark_marks", lambda path: BENCHMARK),
        ):
            try:
                proof_windows.window_summaries(
                    _service(root, object()),
                    {"requests": [CAL, PAIR]},
                    check_cancelled=cancel,
                )
            except ConnectionAbortedError:
                pass
            else:
                raise AssertionError("cancellation must propagate out of window_summaries")
