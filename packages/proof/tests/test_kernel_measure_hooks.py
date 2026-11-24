"""Kernel measurement keeps its public facade as a dynamic patch boundary."""

from unittest.mock import Mock

from moex_spread_lab.core.evaluation_periods import EvaluationSplit, ResearchSettings
from moex_spread_lab.proof import proof_kernel


def test_measure_resolves_admission_hook(monkeypatch):
    admission = Mock(return_value="blocked")
    monkeypatch.setattr(proof_kernel, "_admission", admission)
    split = EvaluationSplit.from_dates(["2026-01-01", "2026-01-02"], 0)
    assert proof_kernel._measure({}, {}, split, ResearchSettings()) == {
        "utility": None,
        "reason": "blocked",
    }
    admission.assert_called_once()
