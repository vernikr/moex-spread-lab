"""Shared helpers for the tuning-screen waiver characterization tests.

The four volume/capital screen combinations exercise the same preflight/search/
live-lamp contract; the assertion bodies live here so each test method stays a
thin scenario description under the code-health limits (P7.2 leaf #134).
"""

import unittest
from typing import Any

from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.proof_batch import checked_plateau
from moex_spread_lab.proof.proof_search import TRIALS, research_run
from moex_spread_lab.proof.service import ProofService
from test_proof_batch import ACTIVE


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
