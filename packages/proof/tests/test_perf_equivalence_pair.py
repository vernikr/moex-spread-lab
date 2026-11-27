"""Pair-screen cohort tier of the bit-for-bit equivalence suite (plan S4a).

The 80-trials-per-strategy pair-screen search, its batch-only and single-run
variants, a checked plateau and window summaries: full resolution, so it runs
behind the ``slow_equiv`` marker in ``--pre-push``, CI and the sanitizer gate.
"""

from __future__ import annotations

import unittest

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.proof.proof_batch import checked_plateau
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.proof_windows import window_summaries
from moex_spread_lab.proof.service import PAIR_UNIVERSE, ProofService
from perf_equivalence_fixtures import H1WorkspaceCase

pytestmark = pytest.mark.slow_equiv


class ProofPerfEquivalencePairTests(H1WorkspaceCase):
    """Pair-screen cohort: optimized vs batch-only vs single run, plateau and windows."""

    def test_1h_pair_screen_cohort_search_plateau_windows_and_single_run(self):
        settings = ResearchSettings(
            timeframe="1h",
            validation_percent=25,
            allow_failed_volume=True,
            allow_failed_capital=False,
            initial_capital_rub=30000.0,
            # Fixed seed: this test compares two runs, which must stay comparable.
            random_seed=41,
        )
        targets = [
            {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"},
            {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRM6"},
        ]

        def suite(service: ProofService):
            cat = service.catalog()
            opt = research_run(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                    "optimize_strategy": True,
                    "targets": targets,
                },
                optimize=True,
            )
            batch_only = research_run(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "fill": "reference_same_bar",
                    "sizing": AUTO_SIZING,
                    "optimize_strategy": False,
                    "targets": targets,
                },
                optimize=True,
            )
            plat = checked_plateau(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "parameters": {"lookback": 12, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                    "targets": targets,
                },
            )
            wins = window_summaries(
                service,
                {
                    "requests": [
                        {
                            "universe": PAIR_UNIVERSE,
                            "pair": t["pair"],
                            "strategy": "mean_reversion",
                            "parameters": {"lookback": 12, "entry_z": 1.2, "exit_z": 0.3},
                            "fill": "next_close",
                            "sizing": AUTO_SIZING,
                        }
                        for t in targets
                    ]
                },
            )
            single = research_run(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "parameters": {"lookback": 12, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                },
                optimize=False,
                include_plateau=True,
            )
            return {
                "catalog": cat,
                "opt": opt,
                "batch_only": batch_only,
                "plateau": plat,
                "windows": wins,
                "single": single,
            }

        res = self._compare_modes(settings, suite)
        self.assertEqual(res["opt"]["schema_version"], "proof-search.v4")
        self.assertEqual(res["opt"]["strategy_search"]["trials_per_strategy"], 80)


if __name__ == "__main__":
    unittest.main()
