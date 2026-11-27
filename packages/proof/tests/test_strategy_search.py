"""Strategy choice is train-only, fixed-cohort and explicit about native execution."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moex_spread_lab.accounting.position_sizing import size_proof
from moex_spread_lab.core.settings import (
    EvaluationSplit,
    ResearchSettings,
    save_settings,
)
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof import proof_kernel
from moex_spread_lab.proof.pair_payload import TRIALS_PER_STRATEGY
from moex_spread_lab.proof.proof_kernel import StudyWindow
from moex_spread_lab.proof.proof_search import _measure, _tune, _validate, research_run
from moex_spread_lab.proof.proof_windows import compact_result
from moex_spread_lab.proof.service import ProofService
from proof_fixtures import funding_winner, rising_calendar, screen_failures
from test_global_capital import FLAT, PAIR, TARGETS
from test_position_sizing import unit_proof


# ---- folded from the retired P7.2 shard strategy_search_reports_p72_packages_proof_tests_test_strategy_search ----
class StrategyReportCases(unittest.TestCase):
    """Report-only winners, holdout isolation, screen gates and the HTTP path."""

    __test__ = False

    root: Path
    service: ProofService

    def search(self, request=PAIR, **extra):
        """Overridden by ``test_strategy_search``, which owns the shared fixtures."""
        raise NotImplementedError

    def test_cross_family_tie_retains_current_and_only_winner_gets_a_report(self):
        reports = []
        # proof_search.StudyWindow is the proof_kernel class re-exported as a
        # Callable type; patch the canonical class so the attribute access types.
        original_report = proof_kernel.StudyWindow.report

        def flat(*args):
            m = _measure(*args)
            return {**m, "rank_score": 0.0, "utility": 0.0}

        def report(case, point):
            reports.append(case.request["strategy"])
            return original_report(case, point)

        with (
            patch.object(proof_kernel, "_measure", side_effect=flat),
            patch.object(proof_kernel.StudyWindow, "report", report),
        ):
            r = self.search({**PAIR, "strategy": "funding_only", "parameters": {"lookback": 8}})
        self.assertEqual(r["selected_strategy"], "funding_only")
        self.assertEqual(reports, ["funding_only"])

    def test_holdout_changes_do_not_choose_a_different_strategy_or_trial_sequence(self):
        save_settings(self.root, {"validation_percent": 25, "allow_failed_volume": False})
        before = self.search()
        path = self.root / "data/w1/candles/CRZ6_1d.json"
        data = json.loads(path.read_text())
        data["rows"][-1]["volume"] = 1
        data["rows"][-1]["close"] += 1
        path.write_text(json.dumps(data))
        # Catastrophically bad *validation* funding must not change which family wins
        # on training, nor leak into its fee reference or parameter proposals.
        path = next((self.root / "data/carry-history-cache").glob("CNYRUBF-*.json"))
        data = json.loads(path.read_text())
        block = data["payload"]["history"]
        for row in block["data"][-12:]:
            row[block["columns"].index("SWAPRATE")] = -5.0
        path.write_text(json.dumps(data))
        after = self.search()
        for key in (
            "candidates",
            "selected_strategy",
            "selected_parameters",
            "plateau",
            "strategy_search",
        ):
            self.assertEqual(before[key], after[key])
        self.assertEqual(before["group"]["training_mean_pp"], after["group"]["training_mean_pp"])
        self.assertEqual(after["selected_strategy"], "funding_only")
        self.assertIsNone(after["group"]["result_mean_pp"])
        self.assertEqual(after["group"]["member_count"], 1)

    def test_both_screen_permissions_still_gate_every_family_on_the_same_fixed_cohort(
        self,
    ):
        self.root = self.root / "screens"
        screen_failures(self.root)
        self.service = ProofService(self.root)
        for volume, capital in (
            (True, True),
            (False, True),
            (True, False),
            (False, False),
        ):
            save_settings(
                self.root,
                {"allow_failed_volume": volume, "allow_failed_capital": capital},
            )
            result = self.search(targets=TARGETS)
            excluded = {
                t["pair"] for t, allow in ((TARGETS[0], volume), (TARGETS[1], capital)) if not allow
            }
            if excluded:
                self.assertEqual(result["status"], "selection_requires_confirmation")
                self.assertEqual({e["target"]["pair"] for e in result["excluded"]}, excluded)
                self.assertEqual(result["trial_count"], 0)
                self.assertEqual(result["remaining_count"], 2 - len(excluded))
            else:
                self.assertEqual(result["status"], "maximum_found")
                self.assertEqual(result["group"]["member_count"], 2)
                self.assertEqual(result["selected_strategy"], "mean_reversion")

    def test_one_capital_split_slippage_snapshot_for_every_family_and_trial(self):
        save_settings(
            self.root,
            {
                "initial_capital_rub": 15000,
                "slippage_ticks": 2.5,
                "validation_percent": 25,
            },
        )
        seen = []

        def change(*args, **kwargs):
            save_settings(
                self.root,
                {
                    "initial_capital_rub": 60000,
                    "slippage_ticks": 0,
                    "validation_percent": 0,
                },
            )
            seen.append(1)
            return _tune(*args, **kwargs)

        with patch("moex_spread_lab.proof.proof_strategy_search._tune", side_effect=change):
            r = self.search()
        self.assertEqual(len(seen), 2)
        for member in r["group"]["members"]:
            self.assertEqual(member["selection"]["initial_capital_rub"], 15000)
            self.assertEqual(member["selection"]["slippage_ticks"], 2.5)
            self.assertEqual(member["evaluation"]["validation_percent"], 25)
        self.assertTrue(
            all(
                "initial_capital_rub" not in c["parameters"]
                and "slippage_ticks" not in c["parameters"]
                for c in r["candidates"]
            )
        )

    def test_cancellation_and_invalid_control_types_fail_closed(self):
        def cancel():
            raise ConnectionAbortedError("cancelled")

        with self.assertRaises(ConnectionAbortedError):
            research_run(
                self.service,
                {**PAIR, "optimize_strategy": True},
                optimize=True,
                check_cancelled=cancel,
            )
        for value in (1, 0, "true", None, []):
            with self.subTest(value=value), self.assertRaises(W0Error):
                self.search(optimize_strategy=value)
        with self.assertRaises(W0Error):
            self.search(fill="not-a-fill")
        for value in ("same_day_settlement", "oops", None):
            with self.assertRaises(W0Error):
                self.search(price_fill=value)
        with self.assertRaises(W0Error):
            research_run(self.service, {**PAIR, "optimize_strategy": True}, optimize=False)
        _validate({**PAIR, "optimize_strategy": False})

    def test_strategy_search_records_every_family_and_returns_run_id_only_for_winner(self):
        result = self.search()
        self.assertEqual(result["selected_strategy"], "funding_only")
        self.assertTrue(result.get("run_id"))
        self.assertEqual(
            {item["strategy"] for item in result["strategy_search"]["alternatives"]},
            {"mean_reversion", "funding_only"},
        )


# ---- folded from the retired P7.2 shard strategy_search_dash_p72_packages_proof_tests_test_strategy_search ----
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


# ---- folded from the retired P7.2 shard strategy_search_cases_p72_packages_proof_tests_test_strategy_search ----
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
            "moex_spread_lab.proof.proof_strategy_search._tune",
            side_effect=AssertionError("no trials before confirmation"),
        ):
            r = self.search(req)
        self.assertEqual(r["status"], "selection_requires_confirmation")
        self.assertEqual(r["remaining_count"], 2)
        self.assertEqual(len(r["excluded"]), 1)
        self.assertEqual(r["trial_count"], 0)
        self.assertIsNone(r["selected_strategy"])

    def test_different_strategy_failures_cannot_shrink_the_denominator(self):
        import moex_spread_lab.proof.proof_strategy_search as module

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
        with patch("moex_spread_lab.proof.proof_kernel._measure", side_effect=fail):
            r = self.search(req)
        self.assertEqual(r["status"], "no_admissible_candidate")
        self.assertEqual(r["trial_count"], 2 * TRIALS_PER_STRATEGY)
        self.assertIsNone(r["selected_parameters"])
        self.assertIsNone(r["selected_strategy"])
        self.assertEqual(req, before)


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
