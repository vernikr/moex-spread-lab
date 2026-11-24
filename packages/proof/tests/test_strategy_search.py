"""Strategy choice is train-only, fixed-cohort and explicit about native execution."""

import tempfile
from pathlib import Path

from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.service import ProofService
from proof_fixtures import funding_winner
from strategy_search_cases_p72_packages_proof_tests_test_strategy_search import (
    StrategyChoiceCases,
)
from strategy_search_dash_p72_packages_proof_tests_test_strategy_search import DashEvidenceCases
from strategy_search_reports_p72_packages_proof_tests_test_strategy_search import (
    StrategyReportCases,
)
from test_global_capital import PAIR


class StrategyChoiceTests(StrategyChoiceCases, StrategyReportCases):
    """Fixture owner: one funded synthetic root per case and the shared seed."""

    __test__ = True

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        funding_winner(self.root)
        # A fixed seed keeps repeated searches comparable; seed 0 would draw a
        # new one per run and these tests compare two searches against each other.
        save_settings(self.root, {"random_seed": 41})
        self.service = ProofService(self.root)

    def search(self, request=PAIR, **extra):
        return research_run(
            self.service, {**request, "optimize_strategy": True, **extra}, optimize=True
        )


class DashEvidenceTests(DashEvidenceCases):
    __test__ = True
