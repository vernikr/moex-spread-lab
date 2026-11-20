"""Tuning exceptions waive two named screens only; they do not fund larger books."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from proof_fixtures import screen_failures
from test_proof_batch import ACTIVE, C, P

from moex_spread_lab.evaluation_periods import (
    ResearchSettings,
    read_settings,
    save_settings,
    settings_path,
)
from moex_spread_lab.proof import ProofService
from moex_spread_lab.proof_batch import checked_plateau
from moex_spread_lab.proof_search import TRIALS, _tune, research_run
from moex_spread_lab.w0 import W0Error


class TuningScreenTests(unittest.TestCase):
    def root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_migration_partial_writes_and_boolean_validation(self):
        root = self.root()
        path = settings_path(root)
        path.parent.mkdir(parents=True)
        legacy = '{"validation_percent": 17.5}\n'
        path.write_text(legacy)
        self.assertEqual(
            read_settings(root),
            {
                "validation_percent": 17.5,
                "allow_failed_volume": True,
                "allow_failed_capital": True,
                "slippage_ticks": 1.0,
                "initial_capital_rub": 30000.0,
                "timeframe": "1d",
                "price_fill": "reference_same_bar",
                "random_seed": 0,
            },
        )
        self.assertEqual(path.read_text(), legacy)
        save_settings(root, {"allow_failed_volume": False})
        save_settings(root, {"validation_percent": 31.5})
        latest = save_settings(root, {"allow_failed_capital": False})
        self.assertEqual(
            latest,
            {
                "allow_failed_volume": False,
                "allow_failed_capital": False,
                "validation_percent": 31.5,
                "slippage_ticks": 1.0,
                "initial_capital_rub": 30000.0,
                "timeframe": "1d",
                "price_fill": "reference_same_bar",
                "random_seed": 0,
            },
        )
        for key in ("allow_failed_volume", "allow_failed_capital"):
            for bad in (None, "false", 1, 0, [], {}):
                with self.subTest(key=key, value=bad), self.assertRaises(W0Error):
                    save_settings(root, {key: bad, "validation_percent": 0})
                self.assertEqual(read_settings(root), latest)

    def test_all_four_combinations_in_preflight_search_report_and_live_lamps(self):
        for thin_calendar, share in ((False, 0), (True, 25)):
            root = self.root()
            screen_failures(root, thin_calendar=thin_calendar)
            svc = ProofService(root)
            req = {**ACTIVE, "targets": [P, C]}
            thin, capital = (C, P) if thin_calendar else (P, C)
            for v, c in ((True, True), (False, True), (True, False), (False, False)):
                with self.subTest(
                    thin_calendar=thin_calendar, allow_volume=v, allow_capital=c
                ):
                    cfg = save_settings(
                        root,
                        {
                            "validation_percent": share,
                            "allow_failed_volume": v,
                            "allow_failed_capital": c,
                        },
                    )
                    excluded = {
                        t["pair"] for t, ok in [(thin, v), (capital, c)] if not ok
                    }
                    preview = checked_plateau(svc, req)
                    r = research_run(svc, req, optimize=True)
                    self.assertEqual(preview["member_count"], 2)
                    for k, val in cfg.items():
                        self.assertEqual(preview[k], val)
                    if excluded:
                        self.assertEqual(r["status"], "selection_requires_confirmation")
                        self.assertEqual(r["trial_count"], 0)
                        self.assertEqual(r["remaining_count"], 2 - len(excluded))
                        self.assertIsNone(preview["plateau"])
                        self.assertEqual(preview["excluded"], r["excluded"])
                        self.assertEqual(
                            {x["target"]["pair"] for x in r["excluded"]}, excluded
                        )
                        # A rejected member does not imply the other, waived (but failing) gate is enforced.
                        if len(excluded) == 1:
                            remaining = thin if v else capital
                            single = research_run(
                                svc, {**req, "targets": [remaining]}, optimize=True
                            )
                            self.assertEqual(single["status"], "maximum_found")
                    else:
                        self.assertEqual(r["status"], "maximum_found")
                        self.assertEqual(r["trial_count"], TRIALS)
                        self.assertEqual(r["group"]["member_count"], 2)
                        self.assertIsNotNone(r["group"]["result_mean_pp"])
                        self.assertEqual(
                            checked_plateau(
                                svc, {**req, "parameters": r["selected_parameters"]}
                            )["plateau"],
                            r["plateau"],
                        )
                        for m in r["group"]["members"]:
                            expected = "volume" if m["target"] == thin else "capital"
                            e = m["evaluation"]
                            self.assertEqual(e["failed_screens"], [expected])
                            self.assertEqual(e["validation_percent"], share)
                            rerun = research_run(svc, m["selection"], optimize=False)
                            self.assertEqual(rerun["evaluation"], e)
                            self.assertEqual(rerun["run_id"], m["run_id"])
                            self.assertFalse(
                                rerun["window"][
                                    "tradable_every_day"
                                    if expected == "volume"
                                    else "passes_capital_screen"
                                ]
                            )
                        # Original stricter raw proof consumer is unaffected by the research settings.
                        with self.assertRaises(W0Error):
                            svc.proof(ACTIVE)

    def test_mid_search_settings_change_only_affects_next_request(self):
        root = self.root()
        screen_failures(root)
        svc = ProofService(root)
        req = {**ACTIVE, "targets": [P, C]}

        def change_while_running(*args, **kwargs):
            save_settings(
                root,
                {
                    "allow_failed_volume": False,
                    "allow_failed_capital": False,
                    "validation_percent": 50,
                },
            )
            return _tune(*args, **kwargs)

        with patch(
            "moex_spread_lab.proof_batch._tune", side_effect=change_while_running
        ):
            r = research_run(svc, req, optimize=True)
        for e in [r["group"], *[m["evaluation"] for m in r["group"]["members"]]]:
            for k, v in ResearchSettings().metadata().items():
                self.assertEqual(e[k], v)
        self.assertIsNotNone(r["group"]["result_mean_pp"])
        next_ = research_run(svc, req, optimize=True)
        self.assertEqual(next_["remaining_count"], 0)
        self.assertEqual(next_["validation_percent"], 50)

    def test_no_free_margin_new_data_or_risk_override(self):
        root = self.root()
        screen_failures(root, thin_calendar=True)
        svc = ProofService(root)
        # ~36k posted margin cannot be financed from the independent 30k account.
        req = {
            **ACTIVE,
            "quantity": 3,
            "parameters": {"lookback": 6, "entry_z": 0.8, "exit_z": 0.1},
        }
        r = research_run(svc, {**req, "targets": [P, C]}, optimize=True)
        self.assertEqual([x["target"] for x in r["excluded"]], [P])
        self.assertIn("превышает стартовый капитал", r["excluded"][0]["reason"])
        # The calendar engine can replay an observed entry, but will not rank a negative-cash book.
        cal = {
            **C,
            "strategy": "zscore_mean_reversion",
            "quantity": 6,
            "parameters": {"window": 10, "entry_z": 1.5, "exit_z": 0.5},
        }
        proof = research_run(svc, cal, optimize=False)
        self.assertGreater(len(proof["trades"]), 0)
        self.assertFalse(proof["window"]["passes_capital_screen"])
        self.assertGreater(proof["trades"][0]["margin_rub"], 30000)
        r = proof["evaluation"]["result"]
        self.assertIn("недостаточны для маржи", r["reason"])
        self.assertIsNone(r.get("annualized_excess_pp"))
        self.assertTrue(
            all(f["score"] is None for f in proof["plateau"]["fields"].values())
        )
        # Missing funding data isn't a volume exception. HTTP flags in a proof request are not an override.
        next((root / "data/carry-history-cache").glob("CNYRUBF-*.json")).unlink()
        r = research_run(svc, {**ACTIVE, "targets": [P, C]}, optimize=True)
        self.assertEqual([x["target"] for x in r["excluded"]], [P])
        self.assertIn("локальных данных", r["excluded"][0]["reason"])
        self.assertEqual(r["trial_count"], 0)
        save_settings(root, {"allow_failed_volume": False})
        r = checked_plateau(
            svc, {**ACTIVE, "targets": [C], "allow_failed_volume": True}
        )
        self.assertIsNone(r["plateau"])
        self.assertFalse(r["allow_failed_volume"])

    def test_two_failed_screens_on_same_carry_window_and_unknown_inputs(self):
        root = self.root()
        screen_failures(root, thin_calendar=True)
        svc = ProofService(root)
        path = next((root / "data/carry-history-cache").glob("CRZ6-*.json"))
        doc = json.loads(path.read_text())
        h = doc["payload"]["history"]
        v = h["columns"].index("VOLUME")
        h["data"][4][v] = 3
        path.write_text(json.dumps(doc))
        req = {
            **P,
            "strategy": "funding_only",
            "parameters": {"lookback": 8},
            "targets": [P],
        }
        r = research_run(svc, req, optimize=True)
        self.assertEqual(r["status"], "maximum_found")
        self.assertEqual(r["evaluation"]["failed_screens"], ["volume", "capital"])
        for vol, cap in ((False, True), (True, False), (False, False)):
            with self.subTest(vol=vol, cap=cap):
                save_settings(
                    root, {"allow_failed_capital": cap, "allow_failed_volume": vol}
                )
                r = research_run(svc, req, optimize=True)
                self.assertEqual(r["remaining_count"], 0)
                self.assertEqual(r["trial_count"], 0)
        save_settings(root, ResearchSettings().metadata())
        for row in h["data"]:
            row[v] = None
        path.write_text(json.dumps(doc))
        r = checked_plateau(svc, req)
        self.assertIsNone(r["plateau"])
        self.assertIn("Нет наблюдений", r["excluded"][0]["reason"])
        # A missing reference margin is not a stress failure with known, financeable GO.
        path = root / "data/w1/current-intake.json"
        doc = json.loads(path.read_text())
        next(i for i in doc["instruments"] if i["secid"] == "SRZ6")[
            "initial_margin"
        ] = None
        path.write_text(json.dumps(doc))
        r = checked_plateau(svc, {**ACTIVE, "targets": [C]})
        self.assertIsNone(r["plateau"])
        self.assertEqual(r["excluded"][0]["target"], C)
