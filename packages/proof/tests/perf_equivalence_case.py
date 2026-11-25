"""Shared test-case base for the proof equivalence tiers (plan S4a).

Every tier clones the same synthetic 1h workspace once per class and compares the
reference evaluator against the fast path bit for bit. The workspace bytes come
from ``perf_equivalence_fixtures``; this module only owns the class lifecycle and
the two-mode comparison so the tiers do not repeat it.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.proof.service import ProofService
from perf_equivalence_fixtures import _build_h1_workspace, _canon_sha


class H1WorkspaceCase(unittest.TestCase):
    """One synthetic 1h workspace per class plus reference-vs-fast comparison."""

    root: Path

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
                expected = fn(ProofService(self.root))
            with patch.dict(os.environ, {"MOEX_PROOF_REFERENCE_EVAL": "0"}):
                actual = fn(ProofService(self.root))
        self.assertEqual(_canon_sha(actual), _canon_sha(expected))
        self.assertEqual(actual, expected)
        return actual
