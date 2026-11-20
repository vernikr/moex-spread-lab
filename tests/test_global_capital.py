"""One persisted capital snapshot drives every proof account, screen and search."""

import json
import math
import tempfile
import unittest
from copy import deepcopy
from datetime import date
from pathlib import Path
from unittest.mock import patch

from proof_fixtures import two_calendar_windows
from test_position_sizing import unit_proof

from moex_spread_lab.evaluation_periods import (
    MAX_RESEARCH_CAPITAL_RUB,
    EvaluationSplit,
    ResearchSettings,
    read_settings,
    save_settings,
    settings_path,
)
from moex_spread_lab.position_sizing import size_proof
from moex_spread_lab.proof import ProofService
from moex_spread_lab.proof_batch import checked_plateau
from moex_spread_lab.proof_search import (
    StudyWindow,
    _annualized_returns,
    _tune,
    research_run,
)
from moex_spread_lab.proof_windows import window_summaries
from moex_spread_lab.screener_app import ScreenerInputs, catalog
from moex_spread_lab.w0 import W0Error

PAIR = {
    "universe": "pair_screen",
    "pair": "CNYRUBF/CRZ6",
    "strategy": "mean_reversion",
    "parameters": {"lookback": 6, "entry_z": 0.8, "exit_z": 0.1},
    "sizing": "max_affordable",
}
CAL = {
    "universe": "calendar_native",
    "asset": "SR",
    "pair": "SRH3/SRM3",
    "strategy": "zscore_mean_reversion",
    "parameters": {"window": 10, "entry_z": 1.5, "exit_z": 0.5},
    "sizing": "max_affordable",
}
FLAT = {**CAL, "pair": "SRM3/SRU3"}
TARGETS = [
    {k: v for k, v in r.items() if k in ("universe", "asset", "pair")}
    for r in (PAIR, CAL)
]


class CapitalSettingsTests(unittest.TestCase):
    def test_default_migration_and_partial_saves_preserve_all_other_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(read_settings(root)["initial_capital_rub"], 30000)
            self.assertFalse(settings_path(root).exists())
            path = settings_path(root)
            path.parent.mkdir(parents=True)
            legacy = '{"validation_percent":12.5,"allow_failed_volume":false,"allow_failed_capital":true,"slippage_ticks":2.5}\n'
            path.write_text(legacy)
            settings = read_settings(root)
            self.assertEqual(settings["initial_capital_rub"], 30000)
            self.assertEqual(path.read_text(), legacy)
            saved = save_settings(root, {"initial_capital_rub": 60000.25})
            self.assertEqual(saved, {**settings, "initial_capital_rub": 60000.25})
            save_settings(root, {"validation_percent": 25})
            save_settings(root, {"slippage_ticks": 3})
            self.assertEqual(read_settings(root)["initial_capital_rub"], 60000.25)
            self.assertFalse(read_settings(root)["allow_failed_volume"])

    def test_invalid_capital_never_replaces_the_saved_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_settings(root, {"initial_capital_rub": 12345.67})
            original = settings_path(root).read_bytes()
            for bad in (
                False,
                True,
                None,
                "30000",
                [],
                {},
                0,
                -1,
                0.001,
                123.456,
                math.nan,
                math.inf,
                -math.inf,
                10**400,
                MAX_RESEARCH_CAPITAL_RUB + 0.01,
            ):
                with self.subTest(value=bad), self.assertRaises(W0Error):
                    save_settings(root, {"initial_capital_rub": bad})
                self.assertEqual(settings_path(root).read_bytes(), original)
            for value in (0.01, 1, 15000, 60000.25, MAX_RESEARCH_CAPITAL_RUB):
                with self.subTest(valid=value):
                    self.assertEqual(
                        save_settings(root, {"initial_capital_rub": value})[
                            "initial_capital_rub"
                        ],
                        value,
                    )

    def test_complete_new_form_can_repair_corrupt_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = settings_path(root)
            path.parent.mkdir(parents=True)
            path.write_text("broken")
            with self.assertRaises(W0Error):
                read_settings(root)
            expected = ResearchSettings(initial_capital_rub=45000).metadata()
            self.assertEqual(save_settings(root, expected), expected)
            self.assertEqual(read_settings(root), expected)


class CapitalReplayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        two_calendar_windows(self.root)
        self.service = ProofService(self.root)

    def run_proof(self, request):
        return research_run(
            self.service, request, optimize=False, include_plateau=False
        )

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
                    self.assertEqual(
                        self.run_proof(p["selection"])["run_id"], p["run_id"]
                    )
                    for trade in p["trades"]:
                        self.assertLessEqual(
                            trade["sizing"]["available_capital_rub"], initial
                        )
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
        p = size_proof(
            unit, marks, ResearchSettings(initial_capital_rub=15000), contracts
        )
        self.assertGreater(p["ledger"][1]["account_value_rub"], 15000)
        self.assertEqual([t["quantity"] for t in p["trades"]], [2, 2])
        self.assertEqual(
            [t["sizing"]["available_capital_rub"] for t in p["trades"]], [15000, 15000]
        )
        p = size_proof(
            unit, marks, ResearchSettings(initial_capital_rub=100), contracts
        )
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
            p = size_proof(
                unit, marks, ResearchSettings(initial_capital_rub=initial), contracts
            )
            # The drawdown is still reported, it just no longer vetoes an entry.
            self.assertGreaterEqual(p["summary"]["max_excess_drawdown_rub"], 10000)
            self.assertGreater(p["ledger"][-1]["account_value_rub"], 0)
            self.assertEqual(p["summary"]["cash_shortfall_rows"], 0)
            if initial == 15000:
                # The account cannot fund a second spread any more.
                self.assertEqual(len(p["trades"]), 1)
                self.assertEqual(len(p["skipped_entries"]), 1)
                self.assertIn(
                    "Недостаточно средств", p["skipped_entries"][0]["reason"]
                )
            else:
                # Capital is left, so the second spread is taken.
                self.assertEqual(len(p["trades"]), 2)
                self.assertEqual(p["skipped_entries"], [])

    def test_nonzero_annualization_uses_configured_capital_and_continuous_validation(
        self,
    ):
        for initial in (15000, 60000):
            for share in (0, 25):
                save_settings(
                    self.root,
                    {"initial_capital_rub": initial, "validation_percent": share},
                )
                for automatic in (False, True):
                    req = {
                        **FLAT,
                        "strategy": "convergence_to_ruonia",
                        "parameters": {"threshold_pp": 0.7},
                    }
                    if not automatic:
                        req.pop("sizing")
                    p = self.run_proof(req)
                    self.assertTrue(p["trades"])
                    # Reconstruct the self-financing account independently, including the
                    # published margin held at the end of each row; never restart at split.
                    source = json.loads(
                        (self.root / "data/benchmark/lqdt-lineage.json").read_text()
                    )
                    prices = {r["date"]: r["price"] for r in source["rows"]}
                    days = [r["date"] for r in p["ledger"]]
                    marks = {d: prices[d] for d in days}
                    values = {}
                    wealth, held, previous_net = initial, 0, 0
                    for i, row in enumerate(p["ledger"]):
                        growth = marks[days[i]] / marks[days[max(0, i - 1)]]
                        wealth += (
                            (wealth - held) * (growth - 1)
                            + row["cumulative_net_rub"]
                            - previous_net
                        )
                        values[row["date"]] = wealth
                        held = (
                            row["posted_margin_rub"]
                            if automatic
                            else sum(
                                t["margin_rub"]
                                for t in p["trades"]
                                if t["entry"]["date"] <= row["date"] < t["exit"]["date"]
                            )
                        )
                        previous_net = row["cumulative_net_rub"]
                    split = EvaluationSplit.from_dates(days, share)
                    anchor = split.training[-1] if share else split.training[0]
                    opening = values[anchor] if share else initial
                    span = (
                        date.fromisoformat(days[-1]) - date.fromisoformat(anchor)
                    ).days
                    expected = 100 * (
                        (values[days[-1]] / opening) ** (365 / span)
                        - (marks[days[-1]] / marks[anchor]) ** (365 / span)
                    )
                    actual = _annualized_returns(p, marks, split)[
                        "annualized_excess_pp"
                    ]
                    with self.subTest(
                        initial=initial, share=share, automatic=automatic
                    ):
                        self.assertNotEqual(expected, 0)
                        self.assertAlmostEqual(actual, expected, places=8)
                        self.assertEqual(
                            actual, p["evaluation"]["result"]["annualized_excess_pp"]
                        )

    def test_zero_trade_lqdt_score_has_the_right_base_with_and_without_holdout(self):
        for initial in (500, 15000, 60000):
            for share in (0, 25):
                save_settings(
                    self.root,
                    {"initial_capital_rub": initial, "validation_percent": share},
                )
                for automatic in (False, True):
                    with self.subTest(
                        capital=initial, share=share, automatic=automatic
                    ):
                        req = deepcopy(FLAT)
                        if not automatic:
                            req.pop("sizing")
                        p = self.run_proof(req)
                        self.assertEqual(p["summary"]["trades"], 0)
                        self.assertEqual(
                            p["evaluation"]["result"]["annualized_excess_pp"], 0
                        )
                        self.assertEqual(p["selection"]["initial_capital_rub"], initial)
                        if automatic:
                            self.assertEqual(
                                p["ledger"][0]["account_value_rub"], initial
                            )

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
            pair = next(
                p for p in data["pair_screen"]["pairs"] if p["id"] == PAIR["pair"]
            )
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
            save_settings(
                self.root, {"initial_capital_rub": initial, "validation_percent": 25}
            )
            requests = [PAIR, CAL, FLAT]
            before = deepcopy(requests)
            compact = window_summaries(self.service, {"requests": requests})
            self.assertEqual(compact["initial_capital_rub"], initial)
            for req, row in zip(requests, compact["rows"]):
                p = self.run_proof(req)
                self.assertEqual(row["run_id"], p["run_id"])
                self.assertEqual(row["trades"], p["summary"]["trades"])
                self.assertEqual(
                    row["annualized_excess_pp"],
                    p["evaluation"]["result"].get("annualized_excess_pp"),
                )
                self.assertEqual(row["evaluation"]["initial_capital_rub"], initial)
            self.assertEqual(requests, before)

    def test_a_table_request_freezes_capital_before_loading_any_member(self):
        save_settings(self.root, {"initial_capital_rub": 20000})

        def change_settings(*args, **kwargs):
            save_settings(self.root, {"initial_capital_rub": 60000})
            return StudyWindow(*args)

        with patch(
            "moex_spread_lab.proof_windows.StudyWindow", side_effect=change_settings
        ):
            result = window_summaries(self.service, {"requests": [PAIR, CAL]})
        self.assertEqual(result["initial_capital_rub"], 20000)
        self.assertTrue(
            all(r["selection"]["initial_capital_rub"] == 20000 for r in result["rows"])
        )
        self.assertEqual(read_settings(self.root)["initial_capital_rub"], 60000)

    def test_search_freezes_capital_and_fill_and_neither_is_an_optimization_axis(self):
        save_settings(self.root, {"initial_capital_rub": 20000})

        def change_settings(*args, **kwargs):
            save_settings(self.root, {"initial_capital_rub": 60000})
            return _tune(*args, **kwargs)

        with patch("moex_spread_lab.proof_batch._tune", side_effect=change_settings):
            result = research_run(
                self.service,
                {**PAIR, "fill": "next_close", "targets": TARGETS},
                optimize=True,
            )
        self.assertEqual(result["status"], "maximum_found")
        self.assertEqual(result["group"]["initial_capital_rub"], 20000)
        self.assertEqual(
            set(result["selected_parameters"]), {"lookback", "entry_z", "exit_z"}
        )
        for member in result["group"]["members"]:
            self.assertEqual(member["selection"]["fill"], "next_close")
            self.assertEqual(member["selection"]["initial_capital_rub"], 20000)
            self.assertEqual(member["evaluation"]["initial_capital_rub"], 20000)
        self.assertEqual(read_settings(self.root)["initial_capital_rub"], 60000)

    def test_legacy_nonresearch_catalog_does_not_implicitly_load_the_preference(self):
        before = catalog(ScreenerInputs(self.root))
        save_settings(self.root, {"initial_capital_rub": 60000})
        self.assertEqual(catalog(ScreenerInputs(self.root)), before)
        self.assertEqual(self.service.catalog()["initial_capital_rub"], 60000)
