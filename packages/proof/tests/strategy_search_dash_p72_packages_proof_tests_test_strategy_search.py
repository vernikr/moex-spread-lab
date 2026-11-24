"""Dash-evidence case extracted from ``test_strategy_search.py`` (P7.2).

The assertions are unchanged. Only the imports moved to module scope: the case now
uses the canonical ``proof_kernel.StudyWindow`` class, which is the same object the
``proof_search`` facade re-exports under a narrower ``Callable`` annotation.
"""

from __future__ import annotations

import unittest
from importlib import import_module
from typing import Any, cast

from moex_spread_lab.accounting.position_sizing import size_proof
from moex_spread_lab.core.settings import EvaluationSplit, ResearchSettings
from moex_spread_lab.proof.proof_kernel import StudyWindow
from moex_spread_lab.proof.proof_windows import compact_result

# ``test_position_sizing`` lives in the accounting test tree, which the workspace
# type-check view does not include; pytest's configured pythonpath resolves it.
unit_proof = cast(Any, import_module("test_position_sizing")).unit_proof


class DashEvidenceCases(unittest.TestCase):
    """An entered trade may later have an unfundable mark, never a zero rate."""

    __test__ = False

    def test_entered_trade_can_later_have_an_unfundable_mark_never_a_zero_rate(self):
        proof, marks, contracts = unit_proof(near=(50000, 48000, 48000, 48000), entries=((0, 3),))
        proof = size_proof(proof, marks, ResearchSettings(), contracts)
        self.assertEqual(len(proof["trades"]), 1)
        self.assertGreater(proof["summary"]["cash_shortfall_rows"], 0)
        proof["instrument"]["family"] = "quarterly_same_asset"
        proof["window"]["tradable_every_day"] = True
        case = object.__new__(StudyWindow)
        case.settings = ResearchSettings()
        case.split = EvaluationSplit.from_dates(marks, 0)
        case.marks = marks
        case.proof = lambda point, training: proof
        report = case.report({})
        result = compact_result(report)
        self.assertEqual(result["trades"], 1)
        self.assertIsNone(result["annualized_excess_pp"])
        evidence = result["evaluation"]["account_checks"]
        self.assertEqual(evidence["first_cash_shortfall_date"], list(marks)[1])
        self.assertLess(evidence["minimum_free_cash_rub"], 0)
        self.assertIn("Свободные средства", result["evaluation"]["result"]["reason"])
