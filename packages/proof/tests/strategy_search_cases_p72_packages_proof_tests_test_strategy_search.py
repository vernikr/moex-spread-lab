"""Search-level strategy cases extracted from ``test_strategy_search.py`` (P7.2).

The facade module keeps discovery, the shared ``setUp``/``search`` helpers and the
historical node IDs (``test_strategy_search.py::StrategyChoiceTests::<case>``).
This mixin only carries the cases that drive the search engine, moved verbatim so
the characterization file fits the code-health size limits.

``__test__ = False`` keeps pytest and unittest from collecting the mixin here; the
facade subclasses it and re-enables collection.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from moex_spread_lab.proof_runtime.proof_search import _measure, research_run
from moex_spread_lab.proof_runtime.service import ProofService
from moex_spread_lab.proof_runtime.trial_budget import TRIALS_PER_STRATEGY
from proof_fixtures import rising_calendar
from test_global_capital import FLAT, PAIR, TARGETS


class StrategyChoiceCases(unittest.TestCase):
    """Cases that only need the facade's ``root``/``service``/``search`` state."""

    __test__ = False

    root: Path
    service: ProofService

    def search(self, request=PAIR, **extra):
        """Overridden by ``test_strategy_search``, which owns the shared fixtures."""
        raise NotImplementedError

    def test_funding_can_replace_mr_with_a_full_budget_for_each_and_native_execution(
        self,
    ):
        r = self.search(fill="reference_same_bar", price_fill="reference_same_bar")
        self.assertEqual(r["selected_strategy"], "funding_only")
        self.assertEqual(r["selected_fill"], "same_day_settlement")
        self.assertEqual(r["schema_version"], "proof-search.v4")
        self.assertEqual(r["trial_count"], 2 * TRIALS_PER_STRATEGY)
        self.assertEqual(len(r["candidates"]), r["trial_count"])
        self.assertEqual([c["trial"] for c in r["candidates"]], list(range(r["trial_count"])))
        self.assertEqual(
            {a["strategy"] for a in r["strategy_search"]["alternatives"]},
            {"mean_reversion", "funding_only"},
        )
        for a in r["strategy_search"]["alternatives"]:
            records = [v for v in r["candidates"] if v["strategy"] == a["strategy"]]
            self.assertEqual(a["trial_count"], TRIALS_PER_STRATEGY)
            self.assertEqual(len(records), TRIALS_PER_STRATEGY)
            self.assertEqual(
                a["training_best_pp"],
                max(v["rank_score"] for v in records if v["rank_score"] is not None),
            )
            self.assertEqual(
                {v["fill"] for v in records},
                {
                    "same_day_settlement"
                    if a["strategy"] == "funding_only"
                    else "reference_same_bar"
                },
            )
            self.assertTrue(
                all(
                    set(v["parameters"])
                    == (
                        {"lookback"}
                        if a["strategy"] == "funding_only"
                        else {"lookback", "entry_z", "exit_z"}
                    )
                    for v in records
                )
            )
        row = r["group"]["members"][0]
        self.assertEqual(row["selection"]["strategy"], "funding_only")
        self.assertFalse(r["group"]["execution_reference"])
        p = research_run(self.service, row["selection"], optimize=False, include_plateau=False)
        self.assertEqual(p["run_id"], row["run_id"])
        self.assertEqual(p["evaluation"], row["evaluation"])
        self.assertEqual(self.search(fill="reference_same_bar", price_fill="reference_same_bar"), r)

    def test_native_convergence_can_replace_mr_instead_of_just_being_enumerated(self):
        rising_calendar(self.root)
        r = self.search(FLAT, fill="next_close")
        self.assertEqual(r["selected_strategy"], "convergence_to_ruonia")
        self.assertEqual(set(r["selected_parameters"]), {"threshold_pp"})
        self.assertEqual(r["selected_fill"], "next_close")
        self.assertEqual(r["group"]["member_count"], 1)
        self.assertGreater(r["group"]["training_mean_pp"], 0)
        p = research_run(self.service, r["group"]["members"][0]["selection"], optimize=False)
        self.assertGreater(len(p["trades"]), 0)
        self.assertEqual(p["summary"]["ledger_reconciliation_rub"], 0)

    def test_mixed_cohort_searches_common_mr_even_when_editor_is_funding_without_dropping(
        self,
    ):
        r = self.search(
            {**PAIR, "strategy": "funding_only", "parameters": {"lookback": 8}},
            targets=TARGETS,
            price_fill="next_close",
        )
        self.assertEqual(r["status"], "maximum_found")
        self.assertEqual(r["selected_strategy"], "mean_reversion")
        self.assertEqual(r["trial_count"], TRIALS_PER_STRATEGY)
        self.assertEqual(r["group"]["member_count"], 2)
        self.assertEqual(r["strategy_search"]["target_count"], 2)
        self.assertEqual(r["strategy_search"]["execution_policy"], "native_rules")
        self.assertEqual(r["strategy_search"]["targets"], [TARGETS[1], TARGETS[0]])
        self.assertNotIn("excluded", r)
        self.assertEqual(
            {row["selection"]["fill"] for row in r["group"]["members"]}, {"next_close"}
        )
        for record in r["candidates"]:
            if record["rank_score"] is not None:
                self.assertEqual(len(record["by_window_pp"]), 2)
                self.assertAlmostEqual(record["rank_score"], sum(record["by_window_pp"]) / 2)
        again = self.search(
            {**PAIR, "strategy": "funding_only", "parameters": {"lookback": 8}},
            targets=list(reversed(TARGETS)),
            price_fill="next_close",
        )
        self.assertEqual(r, again)

    def test_a_bad_strategy_is_skipped_not_its_window_when_another_covers_everyone(
        self,
    ):
        # No near perpetual EOD rows: no funding carry and no MR funding evidence.
        # Instead make only carry's far EOD cache unavailable; candle MR still works.
        for p in (self.root / "data/carry-history-cache").glob("CRZ6-*.json"):
            p.unlink()
        r = self.search(targets=[TARGETS[0]])
        self.assertEqual(r["status"], "maximum_found")
        self.assertEqual(r["selected_strategy"], "mean_reversion")
        self.assertEqual(r["group"]["member_count"], 1)
        bad = next(
            a for a in r["strategy_search"]["alternatives"] if a["strategy"] == "funding_only"
        )
        self.assertEqual(bad["status"], "incompatible")
        self.assertEqual(bad["trial_count"], 0)
        self.assertTrue(bad["excluded"])
        self.assertEqual(r["trial_count"], TRIALS_PER_STRATEGY)

    def test_universally_bad_members_need_confirmation_before_any_fit(self):
        req = {
            **PAIR,
            "targets": [
                *TARGETS,
                {"universe": "pair_screen", "pair": "unknown/unknown"},
            ],
        }
        with patch(
            "moex_spread_lab.proof_runtime.proof_strategy_search._tune",
            side_effect=AssertionError("no trials before confirmation"),
        ):
            r = self.search(req)
        self.assertEqual(r["status"], "selection_requires_confirmation")
        self.assertEqual(r["remaining_count"], 2)
        self.assertEqual(len(r["excluded"]), 1)
        self.assertEqual(r["trial_count"], 0)
        self.assertIsNone(r["selected_strategy"])

    def test_different_strategy_failures_cannot_shrink_the_denominator(self):
        import moex_spread_lab.proof_runtime.proof_strategy_search as module

        # Deliberately disjoint per-family failures. Neither is universally bad.
        def prepare(service, request, settings, benchmark, _cancelled, **kwargs):
            target = (
                TARGETS[0]
                if request["strategy"] == "mean_reversion"
                else {**TARGETS[0], "pair": "CNYRUBF/CRM6"}
            )
            return {
                "excluded": [{"target": target, "reason": "synthetic incompatibility"}],
                "remaining_count": 1,
            }

        targets = [TARGETS[0], {**TARGETS[0], "pair": "CNYRUBF/CRM6"}]
        with (
            patch.object(module, "_prepare_cohort", side_effect=prepare),
            patch.object(
                module,
                "_tune",
                side_effect=AssertionError("never fit a smaller cohort"),
            ),
        ):
            r = self.search(targets=targets)
        self.assertEqual(r["status"], "no_common_strategy")
        self.assertNotIn("excluded", r)
        self.assertEqual(r["trial_count"], 0)
        self.assertIsNone(r["selected_parameters"])

    def test_all_pruned_candidates_leave_strategy_and_parameters_unchanged(self):
        def fail(*args):
            m = _measure(*args)
            return {
                **m,
                "rank_score": None,
                "utility": None,
                "reason": "synthetic unrankable",
            }

        req = {**PAIR, "strategy": "funding_only", "parameters": {"lookback": 8}}
        before = json.loads(json.dumps(req))
        with patch("moex_spread_lab.proof_runtime.proof_search._measure", side_effect=fail):
            r = self.search(req)
        self.assertEqual(r["status"], "no_admissible_candidate")
        self.assertEqual(r["trial_count"], 2 * TRIALS_PER_STRATEGY)
        self.assertIsNone(r["selected_parameters"])
        self.assertIsNone(r["selected_strategy"])
        self.assertEqual(req, before)
