"""Tuning exceptions waive two named screens only; they do not fund larger books."""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from moex_spread_lab.core.settings import (
    ResearchSettings,
    read_settings,
    save_settings,
    settings_path,
)
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.proof_batch import checked_plateau
from moex_spread_lab.proof.proof_search import TRIALS, _tune, research_run
from moex_spread_lab.proof.service import ProofService
from proof_fixtures import screen_failures
from test_proof_batch import ACTIVE, C, P


# ---- folded from the retired P7.2 shard tuning_screen_waiver_helpers_p72_packages_proof_tests_test_tuning_screens ----
def assert_screen_waivers(
    case: unittest.TestCase,
    root: Any,
    svc: ProofService,
    req: dict[str, Any],
    thin: dict[str, Any],
    capital: dict[str, Any],
    share: float,
    *,
    thin_calendar: bool,
) -> None:
    """Check the preflight preview and live lamps for all four waiver flags."""
    for allow_volume, allow_capital in (
        (True, True),
        (False, True),
        (True, False),
        (False, False),
    ):
        with case.subTest(
            thin_calendar=thin_calendar,
            allow_volume=allow_volume,
            allow_capital=allow_capital,
        ):
            cfg = save_settings(
                root,
                {
                    "validation_percent": share,
                    "allow_failed_volume": allow_volume,
                    "allow_failed_capital": allow_capital,
                },
            )
            excluded = {
                target["pair"]
                for target, allowed in ((thin, allow_volume), (capital, allow_capital))
                if not allowed
            }
            preview = checked_plateau(svc, req)
            result = research_run(svc, req, optimize=True)
            case.assertEqual(preview["member_count"], 2)
            for key, value in cfg.items():
                case.assertEqual(preview[key], value)
            if excluded:
                _assert_excluded_members(
                    case, preview, result, excluded, svc, req, thin, allow_volume, capital
                )
            else:
                _assert_selected_book(case, result, share, svc, req, thin)


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
            assert_screen_waivers(
                self, root, svc, req, thin, capital, share, thin_calendar=thin_calendar
            )

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

        with patch("moex_spread_lab.proof.proof_batch._tune", side_effect=change_while_running):
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
        self.assertTrue(all(f["score"] is None for f in proof["plateau"]["fields"].values()))
        # Missing funding data isn't a volume exception. HTTP flags in a proof request are not an override.
        next((root / "data/carry-history-cache").glob("CNYRUBF-*.json")).unlink()
        r = research_run(svc, {**ACTIVE, "targets": [P, C]}, optimize=True)
        self.assertEqual([x["target"] for x in r["excluded"]], [P])
        self.assertIn("локальных данных", r["excluded"][0]["reason"])
        self.assertEqual(r["trial_count"], 0)
        save_settings(root, {"allow_failed_volume": False})
        r = checked_plateau(svc, {**ACTIVE, "targets": [C], "allow_failed_volume": True})
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
                save_settings(root, {"allow_failed_capital": cap, "allow_failed_volume": vol})
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
        next(i for i in doc["instruments"] if i["secid"] == "SRZ6")["initial_margin"] = None
        path.write_text(json.dumps(doc))
        r = checked_plateau(svc, {**ACTIVE, "targets": [C]})
        self.assertIsNone(r["plateau"])
        self.assertEqual(r["excluded"][0]["target"], C)


def _assert_excluded_members(
    case: unittest.TestCase,
    preview: dict[str, Any],
    result: dict[str, Any],
    excluded: set[str],
    svc: ProofService,
    req: dict[str, Any],
    thin: dict[str, Any],
    allow_volume: bool,
    capital: dict[str, Any],
) -> None:
    case.assertEqual(result["status"], "selection_requires_confirmation")
    case.assertEqual(result["trial_count"], 0)
    case.assertEqual(result["remaining_count"], 2 - len(excluded))
    case.assertIsNone(preview["plateau"])
    case.assertEqual(preview["excluded"], result["excluded"])
    case.assertEqual({x["target"]["pair"] for x in result["excluded"]}, excluded)
    # A rejected member does not imply the other, waived (but failing) gate is enforced.
    if len(excluded) == 1:
        remaining = thin if allow_volume else capital
        single = research_run(svc, {**req, "targets": [remaining]}, optimize=True)
        case.assertEqual(single["status"], "maximum_found")


def _assert_selected_book(
    case: unittest.TestCase,
    result: dict[str, Any],
    share: float,
    svc: ProofService,
    req: dict[str, Any],
    thin: dict[str, Any],
) -> None:
    case.assertEqual(result["status"], "maximum_found")
    case.assertEqual(result["trial_count"], TRIALS)
    case.assertEqual(result["group"]["member_count"], 2)
    case.assertIsNotNone(result["group"]["result_mean_pp"])
    case.assertEqual(
        checked_plateau(svc, {**req, "parameters": result["selected_parameters"]})["plateau"],
        result["plateau"],
    )
    for member in result["group"]["members"]:
        _assert_member_lamps(case, member, thin, share, svc)
    # Original stricter raw proof consumer is unaffected by the research settings.
    with case.assertRaises(W0Error):
        svc.proof(ACTIVE)


def _assert_member_lamps(
    case: unittest.TestCase,
    member: dict[str, Any],
    thin: dict[str, Any],
    share: float,
    svc: ProofService,
) -> None:
    expected = "volume" if member["target"] == thin else "capital"
    evaluation = member["evaluation"]
    case.assertEqual(evaluation["failed_screens"], [expected])
    case.assertEqual(evaluation["validation_percent"], share)
    rerun = research_run(svc, member["selection"], optimize=False)
    case.assertEqual(rerun["evaluation"], evaluation)
    case.assertEqual(rerun["run_id"], member["run_id"])
    window = rerun["window"]
    failed = (
        window["tradable_every_day"] if expected == "volume" else window["passes_capital_screen"]
    )
    case.assertFalse(failed)
