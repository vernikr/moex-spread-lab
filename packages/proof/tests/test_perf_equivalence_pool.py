"""Worker-pool and streaming tier of the bit-for-bit equivalence suite (plan S4a).

Spawn-pool parity (sequential vs forced pool), cooperative cancellation followed by
a healthy retry, and the NDJSON streaming progress path over a loopback server.
All three exercise execution underneath the fast path, so they run behind the
``slow_equiv`` marker in ``--pre-push``, the CI perf-equivalence job and the
ASan/UBSan gate, never in default discovery.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.proof_runtime.proof_batch import checked_plateau
from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.proof_runtime.proof_windows import window_summaries
from moex_spread_lab.proof_runtime.service import CALENDAR_UNIVERSE, PAIR_UNIVERSE, ProofService
from perf_equivalence_fixtures import (  # noqa: F401  (re-exported helper names)
    _build_h1_workspace,
    _canon,
    _canon_sha,
    _write_h1_candles,
)
from perf_equivalence_pool_fixtures import _build_pool_test_workspace

pytestmark = pytest.mark.slow_equiv


class ProofPerfEquivalencePoolTests(unittest.TestCase):
    """Worker pool and streaming progress must match the sequential path bit for bit."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        _build_h1_workspace(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _compare_modes(self, settings: ResearchSettings, fn):
        with patch.object(ResearchSettings, "load", return_value=settings):
            with patch.dict(os.environ, {"MOEX_PROOF_REFERENCE_EVAL": "1"}):
                ref_service = ProofService(self.root)
                expected = fn(ref_service)
            with patch.dict(os.environ, {"MOEX_PROOF_REFERENCE_EVAL": "0"}):
                fast_service = ProofService(self.root)
                actual = fn(fast_service)
        self.assertEqual(_canon_sha(actual), _canon_sha(expected))
        self.assertEqual(actual, expected)
        return actual

    def test_spawn_pool_matches_sequential_bit_for_bit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_pool_test_workspace(root)
            targets = [
                {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"},
                {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRM6"},
                {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRU6"},
                {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"},
            ]
            batch_req = {
                "universe": PAIR_UNIVERSE,
                "pair": "CNYRUBF/CRZ6",
                "strategy": "mean_reversion",
                "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                "fill": "next_open",
                "sizing": AUTO_SIZING,
                "targets": targets,
            }
            strat_req = {**batch_req, "optimize_strategy": True}
            win_req = {
                "requests": [
                    {
                        "universe": PAIR_UNIVERSE,
                        "pair": "CNYRUBF/CRZ6",
                        "strategy": "mean_reversion",
                        "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                        "fill": "next_open",
                        "sizing": AUTO_SIZING,
                    },
                    {
                        "universe": PAIR_UNIVERSE,
                        "pair": "CNYRUBF/CRM6",
                        "strategy": "mean_reversion",
                        "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                        "fill": "next_open",
                        "sizing": AUTO_SIZING,
                    },
                    {
                        "universe": PAIR_UNIVERSE,
                        "pair": "CNYRUBF/CRU6",
                        "strategy": "mean_reversion",
                        "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                        "fill": "next_open",
                        "sizing": AUTO_SIZING,
                    },
                    {
                        "universe": CALENDAR_UNIVERSE,
                        "asset": "SR",
                        "pair": "SRH3/SRM3",
                        "strategy": "zscore_mean_reversion",
                        "parameters": {"window": 10, "entry_z": 1.2, "exit_z": 0.3},
                        "fill": "next_open",
                        "sizing": AUTO_SIZING,
                    },
                ]
            }

            def run_mode(workers: str, force: str):
                with patch.dict(
                    os.environ,
                    {"MOEX_PROOF_WORKERS": workers, "MOEX_PROOF_FORCE_POOL": force},
                ):
                    svc = ProofService(root)
                    try:
                        return {
                            "batch": research_run(svc, batch_req, optimize=True),
                            "strategy": research_run(svc, strat_req, optimize=True),
                            "plateau": checked_plateau(svc, batch_req),
                            "windows": window_summaries(svc, win_req),
                        }
                    finally:
                        svc.close()

            seq = run_mode("1", "0")
            par = run_mode("3", "1")
            self.assertEqual(seq["batch"]["status"], "maximum_found")
            self.assertEqual(seq["batch"]["group"]["member_count"], 4)
            self.assertEqual(_canon_sha(par), _canon_sha(seq))
            self.assertEqual(par, seq)

    def test_spawn_pool_cooperative_cancellation_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_pool_test_workspace(root)
            req = {
                "universe": PAIR_UNIVERSE,
                "pair": "CNYRUBF/CRZ6",
                "strategy": "mean_reversion",
                "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                "fill": "next_open",
                "sizing": AUTO_SIZING,
                "targets": [
                    {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"},
                    {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRM6"},
                    {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRU6"},
                    {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"},
                ],
            }
            with patch.dict(
                os.environ,
                {"MOEX_PROOF_WORKERS": "3", "MOEX_PROOF_FORCE_POOL": "1"},
            ):
                svc = ProofService(root)
                try:
                    calls = 0

                    def cancel_soon():
                        nonlocal calls
                        calls += 1
                        if calls >= 12:
                            raise ConnectionAbortedError("client disconnected")

                    with self.assertRaises(ConnectionAbortedError):
                        research_run(svc, req, optimize=True, check_cancelled=cancel_soon)

                    # Pool remains healthy and produces the exact canonical result after cancellation.
                    recovered = research_run(svc, req, optimize=True)
                    self.assertIn("run_id", recovered)
                    self.assertEqual(recovered["status"], "maximum_found")
                    self.assertEqual(recovered["group"]["member_count"], 4)
                finally:
                    svc.close()
