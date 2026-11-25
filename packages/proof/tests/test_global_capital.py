"""One persisted capital snapshot drives every proof account, screen and search."""

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path

from moex_spread_lab.accounting.position_sizing import size_proof
from moex_spread_lab.core.settings import EvaluationSplit, ResearchSettings, save_settings
from moex_spread_lab.proof.proof_batch import checked_plateau
from moex_spread_lab.proof.proof_search import _annualized_returns, research_run
from moex_spread_lab.proof.proof_windows import window_summaries
from moex_spread_lab.proof.service import ProofService
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs, catalog
from proof_fixtures import CAL, FLAT, PAIR, two_calendar_windows
from test_position_sizing import unit_proof


# ---- folded from the retired P7.2 shard global_capital_replay_p72_packages_proof_tests_test_global_capital ----
def published_marks(root, ledger):
    """LQDT closes for exactly the dates the proof ledger reports."""
    source = json.loads((root / LINEAGE_PATH).read_text())
    prices = {row["date"]: row["price"] for row in source["rows"]}
    return {row["date"]: prices[row["date"]] for row in ledger}


def reconstruct_wealth(ledger, marks, trades, initial, automatic):
    """Wealth after each ledger row, growing only the part not held as margin."""
    days = [row["date"] for row in ledger]
    values = {}
    wealth, held, previous_net = initial, 0, 0
    for i, row in enumerate(ledger):
        growth = marks[days[i]] / marks[days[max(0, i - 1)]]
        wealth += (wealth - held) * (growth - 1) + row["cumulative_net_rub"] - previous_net
        values[row["date"]] = wealth
        held = held_margin(row, trades, automatic)
        previous_net = row["cumulative_net_rub"]
    return values, days


def expected_excess_pp(values, marks, days, initial, share):
    """Two-legged annualization between the split anchor and the last published day."""
    split = EvaluationSplit.from_dates(days, share)
    anchor = split.training[-1] if share else split.training[0]
    opening = values[anchor] if share else initial
    span = (date.fromisoformat(days[-1]) - date.fromisoformat(anchor)).days
    return 100 * (
        (values[days[-1]] / opening) ** (365 / span)
        - (marks[days[-1]] / marks[anchor]) ** (365 / span)
    )


def runtime_excess_pp(proof, marks, days, share):
    """Runtime annualized excess on the evaluation split rebuilt from the ledger days."""
    split = EvaluationSplit.from_dates(days, share)
    return _annualized_returns(proof, marks, split)["annualized_excess_pp"]


TARGETS = [{k: v for k, v in r.items() if k in ("universe", "asset", "pair")} for r in (PAIR, CAL)]


class CapitalReplayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        two_calendar_windows(self.root)
        self.service = ProofService(self.root)

    def run_proof(self, request):
        return research_run(self.service, request, optimize=False, include_plateau=False)

    def run_sized(self, request, initial, share, automatic):
        """Persist the capital and holdout choice, then drop explicit sizing if asked."""
        save_settings(self.root, {"initial_capital_rub": initial, "validation_percent": share})
        req = deepcopy(request)
        if not automatic:
            req.pop("sizing")
        return self.run_proof(req)

    def test_all_four_strategies_use_the_budget_and_never_a_request_override(self):
        requests = (
            PAIR,
            CAL,
            {**PAIR, "strategy": "funding_only", "parameters": {"lookback": 8}},
            {
                **FLAT,
                "strategy": "convergence_to_ruonia",
                "parameters": {"threshold_pp": 0.7},
            },
        )
        ids = {r["strategy"]: [] for r in requests}
        quantities = {}
        for initial in (15000, 30000, 60000):
            save_settings(self.root, {"initial_capital_rub": initial})
            quantities[initial] = {}
            for request in requests:
                with self.subTest(capital=initial, strategy=request["strategy"]):
                    p = self.run_proof({**request, "initial_capital_rub": 99999999})
                    self.assertEqual(p["initial_capital_rub"], initial)
                    self.assertEqual(p["selection"]["initial_capital_rub"], initial)
                    self.assertEqual(p["evaluation"]["initial_capital_rub"], initial)
                    self.assertEqual(p["sizing"]["entry_capital_ceiling_rub"], initial)
                    self.assertEqual(p["sizing"]["additional_funding_rub"], 0)
                    self.assertEqual(p["summary"]["ledger_reconciliation_rub"], 0)
                    self.assertEqual(self.run_proof(p["selection"])["run_id"], p["run_id"])
                    for trade in p["trades"]:
                        self.assertLessEqual(trade["sizing"]["available_capital_rub"], initial)
                    ids[request["strategy"]].append(p["run_id"])
                    quantities[initial][request["strategy"]] = (
                        p["trades"][0]["quantity"] if p["trades"] else 0
                    )
        self.assertTrue(all(len(set(values)) == 3 for values in ids.values()))
        for strategy in ids:
            self.assertLess(quantities[15000][strategy], quantities[30000][strategy])
            self.assertLess(quantities[30000][strategy], quantities[60000][strategy])

    def test_profits_do_not_expand_a_nondefault_envelope_and_small_budgets_skip(self):
        unit, marks, contracts = unit_proof(near=(50000, 50200, 50200, 50200))
        p = size_proof(unit, marks, ResearchSettings(initial_capital_rub=15000), contracts)
        self.assertGreater(p["ledger"][1]["account_value_rub"], 15000)
        self.assertEqual([t["quantity"] for t in p["trades"]], [2, 2])
        self.assertEqual(
            [t["sizing"]["available_capital_rub"] for t in p["trades"]], [15000, 15000]
        )
        p = size_proof(unit, marks, ResearchSettings(initial_capital_rub=100), contracts)
        self.assertEqual(p["trades"], [])
        self.assertTrue(p["skipped_entries"])
        self.assertTrue(all(r["account_value_rub"] == 100 for r in p["ledger"]))
        self.assertEqual(p["summary"]["costs_rub"], 0)

    def test_nondefault_budgets_keep_reporting_drawdown_without_a_risk_trigger(
        self,
    ):
        # The benchmark-relative 10k drawdown trigger no longer stops entries at
        # any starting capital: a later entry is attempted and is taken whenever
        # the account can still fund it.
        for initial, loss_per_spread in ((15000, 5100), (60000, 1000)):
            unit, marks, contracts = unit_proof(
                near=(
                    50000,
                    50000 - loss_per_spread,
                    50000 - loss_per_spread,
                    50000 - loss_per_spread,
                )
            )
            p = size_proof(unit, marks, ResearchSettings(initial_capital_rub=initial), contracts)
            # The drawdown is still reported, it just no longer vetoes an entry.
            self.assertGreaterEqual(p["summary"]["max_excess_drawdown_rub"], 10000)
            self.assertGreater(p["ledger"][-1]["account_value_rub"], 0)
            self.assertEqual(p["summary"]["cash_shortfall_rows"], 0)
            if initial == 15000:
                # The account cannot fund a second spread any more.
                self.assertEqual(len(p["trades"]), 1)
                self.assertEqual(len(p["skipped_entries"]), 1)
                self.assertIn("Недостаточно средств", p["skipped_entries"][0]["reason"])
            else:
                # Capital is left, so the second spread is taken.
                self.assertEqual(len(p["trades"]), 2)
                self.assertEqual(p["skipped_entries"], [])

    def test_nonzero_annualization_uses_configured_capital_and_continuous_validation(
        self,
    ):
        for initial in (15000, 60000):
            for share in (0, 25):
                for automatic in (False, True):
                    with self.subTest(initial=initial, share=share, automatic=automatic):
                        self.check_annualization(initial, share, automatic)

    def check_annualization(self, initial, share, automatic):
        """Independent reconstruction and the runtime score agree for this case."""
        p = self.run_sized(
            {**FLAT, "strategy": "convergence_to_ruonia", "parameters": {"threshold_pp": 0.7}},
            initial,
            share,
            automatic,
        )
        self.assertTrue(p["trades"])
        # Reconstruct the self-financing account independently, including the
        # published margin held at the end of each row; never restart at split.
        marks = published_marks(self.root, p["ledger"])
        values, days = reconstruct_wealth(p["ledger"], marks, p["trades"], initial, automatic)
        expected = expected_excess_pp(values, marks, days, initial, share)
        actual = runtime_excess_pp(p, marks, days, share)
        self.assertNotEqual(expected, 0)
        self.assertAlmostEqual(actual, expected, places=8)
        self.assertEqual(actual, p["evaluation"]["result"]["annualized_excess_pp"])

    def test_zero_trade_lqdt_score_has_the_right_base_with_and_without_holdout(self):
        for initial in (500, 15000, 60000):
            for share in (0, 25):
                for automatic in (False, True):
                    with self.subTest(capital=initial, share=share, automatic=automatic):
                        p = self.run_sized(FLAT, initial, share, automatic)
                        self.assertEqual(p["summary"]["trades"], 0)
                        self.assertEqual(p["evaluation"]["result"]["annualized_excess_pp"], 0)
                        self.assertEqual(p["selection"]["initial_capital_rub"], initial)
                        if automatic:
                            self.assertEqual(p["ledger"][0]["account_value_rub"], initial)

    def test_current_capital_changes_both_catalog_checks_and_strict_group_admission(
        self,
    ):
        req = {**PAIR, "targets": TARGETS}
        for initial, excluded in ((15000, 2), (60000, 0)):
            save_settings(
                self.root,
                {"initial_capital_rub": initial, "allow_failed_capital": False},
            )
            data = self.service.catalog()
            pair = next(p for p in data["pair_screen"]["pairs"] if p["id"] == PAIR["pair"])
            native = next(
                w
                for a in data["assets"]
                if a["asset"] == "SR"
                for w in a["windows"]
                if w["pair"] == CAL["pair"]
            )
            self.assertEqual(pair["passes_capital_screen"], not excluded)
            self.assertEqual(native["passes_capital_screen"], not excluded)
            live = checked_plateau(self.service, req)
            self.assertEqual(len(live.get("excluded", [])), excluded)
            self.assertEqual(live["initial_capital_rub"], initial)
            self.assertEqual(live["member_count"], 2)
            self.assertEqual(live["status"], "unavailable" if excluded else "measured")

    def test_compact_rows_match_the_current_chart_and_are_not_a_previous_capital(self):
        for initial in (15000, 60000):
            save_settings(self.root, {"initial_capital_rub": initial, "validation_percent": 25})
            requests = [PAIR, CAL, FLAT]
            before = deepcopy(requests)
            compact = window_summaries(self.service, {"requests": requests})
            self.assertEqual(compact["initial_capital_rub"], initial)
            for req, row in zip(requests, compact["rows"], strict=True):
                p = self.run_proof(req)
                self.assertEqual(row["run_id"], p["run_id"])
                self.assertEqual(row["trades"], p["summary"]["trades"])
                self.assertEqual(
                    row["annualized_excess_pp"],
                    p["evaluation"]["result"].get("annualized_excess_pp"),
                )
                self.assertEqual(row["evaluation"]["initial_capital_rub"], initial)
            self.assertEqual(requests, before)

    def test_legacy_nonresearch_catalog_does_not_implicitly_load_the_preference(self):
        before = catalog(ScreenerInputs(self.root))
        save_settings(self.root, {"initial_capital_rub": 60000})
        self.assertEqual(catalog(ScreenerInputs(self.root)), before)
        self.assertEqual(self.service.catalog()["initial_capital_rub"], 60000)


LINEAGE_PATH = Path("data/benchmark/lqdt-lineage.json")


def held_margin(row, trades, automatic):
    """Margin posted at the end of one ledger row."""
    if automatic:
        return row["posted_margin_rub"]
    return sum(
        trade["margin_rub"]
        for trade in trades
        if trade["entry"]["date"] <= row["date"] < trade["exit"]["date"]
    )
