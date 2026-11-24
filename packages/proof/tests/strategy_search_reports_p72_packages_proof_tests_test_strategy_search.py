"""Cohort, holdout and HTTP cases extracted from ``test_strategy_search.py`` (P7.2).

Moved verbatim so the characterization file fits the code-health size limits; node
IDs stay on ``test_strategy_search.py`` because the facade subclasses this mixin
with ``__test__ = True``.
"""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime import proof_kernel, proof_search
from moex_spread_lab.proof_runtime.proof_search import _measure, _tune, _validate, research_run
from moex_spread_lab.proof_runtime.service import ProofService
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs
from moex_spread_lab.web.server import ScreenerStore, make_handler
from proof_fixtures import screen_failures
from test_global_capital import PAIR, TARGETS


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
            patch.object(proof_search, "_measure", side_effect=flat),
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

        with patch("moex_spread_lab.proof_runtime.proof_strategy_search._tune", side_effect=change):
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

    def test_http_ui_path_records_every_family_and_only_the_final_selection(self):
        store = ScreenerStore(ScreenerInputs(self.root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=20)
        self.addCleanup(connection.close)
        connection.request(
            "POST",
            "/api/proof/optimize",
            json.dumps({**PAIR, "optimize_strategy": True}),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        result = json.load(response)
        self.assertEqual(result["selected_strategy"], "funding_only")
        logs = (self.root / "data/analytics/proof-searches.jsonl").read_text().splitlines()
        self.assertEqual(len(logs), 1)
        logged = json.loads(logs[0])
        self.assertTrue(logged.pop("recorded_at"))
        self.assertEqual(logged, result)
