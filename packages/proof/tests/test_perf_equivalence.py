"""Fast canary tier of the bit-for-bit equivalence suite (plan S4a).

Runs in every default discovery. It keeps a real cohort search, the 1d
funding/calendar parity, the 1h calendar cohort and the two cheap invariants
(cache invalidation, reference-eval switch). The pair-screen cohort lives in
``test_perf_equivalence_pair.py`` and the worker-pool, C-fallback and
streaming cases in ``test_perf_equivalence_full.py``; all three run in
``--pre-push``, the CI perf-equivalence job and the ASan/UBSan gate.
Test IDs are preserved, and no case was deleted or had its trial count trimmed.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers_p72_packages_proof_tests_test_perf_equivalence import (
    calendar_spread_request,
    convergence_window_requests,
    pair_screen_fixed_quantity_request,
    pair_screen_request,
    research_settings,
    rewrite_same_size,
)
from moex_spread_lab.core.settings import ResearchSettings, save_settings
from moex_spread_lab.proof.proof_batch import checked_plateau
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.proof_windows import window_summaries
from moex_spread_lab.proof.service import (
    CALENDAR_UNIVERSE,
    PAIR_UNIVERSE,
    ProofService,
    reference_eval,
)
from moex_spread_lab.strategies.calendar.calendar_spreads import HISTORY_FIELDS, HISTORY_FROM
from moex_spread_lab.strategies.funding_carry.carry import fetch_history
from perf_equivalence_fixtures import (  # noqa: F401  (re-exported helper names)
    _build_h1_workspace,
    _canon,
    _canon_sha,
    _write_h1_candles,
)
from proof_fixtures import funding_winner


class ProofPerfEquivalenceTests(unittest.TestCase):
    """Canary slice: the default-discovery contract of the equivalence suite."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        _build_h1_workspace(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _compare_modes(self, settings: ResearchSettings, fn):
        with patch.object(ResearchSettings, "load", return_value=settings):
            with patch.dict(os.environ, {"MOEX_PROOF_REFERENCE_EVAL": "1"}):
                ref_service = ProofService(self.root)
                expected = fn(ref_service)
            with patch.dict(os.environ, {"MOEX_PROOF_REFERENCE_EVAL": "0"}):
                fast_service = ProofService(self.root)
                actual = fn(fast_service)
        self.assertEqual(_canon_sha(actual), _canon_sha(expected))
        self.assertEqual(actual, expected)
        return actual

    def test_1h_calendar_proxy_and_native_cohort_and_mixed_universe(self):
        settings = research_settings(timeframe="1h", initial_capital_rub=100000.0)
        cal_targets = [
            {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"},
            {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRM3/SRU3"},
        ]
        mixed_targets = [
            {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"},
            {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"},
        ]

        def suite(service: ProofService):
            cal_opt = research_run(
                service,
                calendar_spread_request(optimize_strategy=True, targets=cal_targets),
                optimize=True,
            )
            mixed_opt = research_run(
                service,
                pair_screen_request(optimize_strategy=True, targets=mixed_targets),
                optimize=True,
            )
            plat = checked_plateau(
                service,
                calendar_spread_request(
                    parameters={"window": 10, "entry_z": 1.2, "exit_z": 0.3},
                    targets=cal_targets,
                ),
            )
            wins = window_summaries(
                service,
                {"requests": convergence_window_requests(cal_targets)},
            )
            return {
                "cal_opt": cal_opt,
                "mixed_opt": mixed_opt,
                "plateau": plat,
                "windows": wins,
            }

        res = self._compare_modes(settings, suite)
        self.assertEqual(res["cal_opt"]["schema_version"], "proof-search.v4")
        self.assertEqual(len(res["cal_opt"]["strategy_search"]["alternatives"]), 2)
        self.assertEqual(res["mixed_opt"]["schema_version"], "proof-search.v4")

    def test_1d_pair_funding_and_calendar_and_fixed_quantity_equivalence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            funding_winner(root)
            settings = research_settings(timeframe="1d", initial_capital_rub=30000.0)

            def run_for(service: ProofService):
                auto_pair = research_run(
                    service,
                    pair_screen_request(
                        optimize_strategy=True,
                        targets=[{"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"}],
                    ),
                    optimize=True,
                )
                fixed_pair = research_run(
                    service,
                    pair_screen_fixed_quantity_request(
                        optimize_strategy=False,
                        targets=[{"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"}],
                    ),
                    optimize=True,
                )
                cal_1d = research_run(
                    service,
                    calendar_spread_request(
                        optimize_strategy=True,
                        targets=[
                            {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"}
                        ],
                    ),
                    optimize=True,
                )
                return {"auto_pair": auto_pair, "fixed_pair": fixed_pair, "cal_1d": cal_1d}

            self._compare_modes(settings, run_for)

    def test_file_caches_invalidate_immediately_on_in_place_same_size_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_h1_workspace(root)
            save_settings(
                root,
                {
                    "timeframe": "1h",
                    "validation_percent": 0,
                    "allow_failed_volume": True,
                    "allow_failed_capital": True,
                    "initial_capital_rub": 100000.0,
                },
            )
            service = ProofService(root)

            # 1. Warm pair + calendar caches via catalog and proof runs.
            cat_before = service.catalog()
            pair_params = {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3}
            cal_params = {"window": 10, "entry_z": 1.2, "exit_z": 0.3}
            pair_before = research_run(
                service, pair_screen_request(parameters=pair_params), optimize=False
            )
            cal_before = research_run(
                service, calendar_spread_request(parameters=cal_params), optimize=False
            )

            # 2. Mutate CRZ6_1h.json with the EXACT same byte length. A fresh
            #    (path, st_mtime_ns, st_size) key must invalidate the cache.
            crz6_path = root / "data/w1/candles-screen/CRZ6_1h.json"
            rewrite_same_size(crz6_path, '"volume": 60', '"volume": 61')
            pair_after = research_run(
                service, pair_screen_request(parameters=pair_params), optimize=False
            )
            self.assertNotEqual(
                pair_before["provenance"]["far_candles_sha256"],
                pair_after["provenance"]["far_candles_sha256"],
            )
            self.assertNotEqual(pair_before["run_id"], pair_after["run_id"])

            # 3. Mutate the SRH3 EOD history cache file with the EXACT same byte
            #    length and verify the shared CalendarData picks up the fresh
            #    mtime/size key (calendar proofs read EOD history, not 1h candles,
            #    for near_history_sha256).
            eod_candidates = sorted(root.glob("data/spread-history-cache/SRH3-*-0.json"))
            self.assertTrue(eod_candidates, "SRH3 EOD history cache present")
            srh3_eod = eod_candidates[0]
            raw_srh3 = srh3_eod.read_text(encoding="utf-8")
            # Row 0 settles at 30000.0; replace the first occurrence with a
            # same-length value so st_size is unchanged but raw bytes differ.
            self.assertIn("30000.0", raw_srh3)
            mutated_srh3 = rewrite_same_size(srh3_eod, "30000.0", "30001.0")
            cal_after = research_run(
                service, calendar_spread_request(parameters=cal_params), optimize=False
            )
            self.assertNotEqual(
                cal_before["provenance"]["near_history_sha256"],
                cal_after["provenance"]["near_history_sha256"],
            )
            self.assertNotEqual(cal_before["run_id"], cal_after["run_id"])
            cat_after = service.catalog()
            self.assertIsNotNone(cat_after)
            self.assertEqual(cat_before["timeframe"], cat_after["timeframe"])

            # 4. Also verify carry.fetch_history's dedicated mtime/size cache
            #    observes a same-size in-place edit immediately.
            till_str = "-".join(srh3_eod.name.split("-")[4:7])
            before_hist = fetch_history(
                "SRH3",
                HISTORY_FROM,
                till_str,
                cache_dir=root / "data/spread-history-cache",
                delay_seconds=0,
                fields=HISTORY_FIELDS,
            )
            mutated_srh3_2 = rewrite_same_size(srh3_eod, "30001.0", "30002.0")
            self.assertEqual(len(mutated_srh3), len(mutated_srh3_2))
            after_hist = fetch_history(
                "SRH3",
                HISTORY_FROM,
                till_str,
                cache_dir=root / "data/spread-history-cache",
                delay_seconds=0,
                fields=HISTORY_FIELDS,
            )
            self.assertNotEqual(before_hist["rows"][0], after_hist["rows"][0])

    def test_default_reference_eval_is_fast_while_direct_through_replay_stays_full(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOEX_PROOF_REFERENCE_EVAL", None)
            self.assertFalse(reference_eval())
        with patch.dict(os.environ, {"MOEX_PROOF_REFERENCE_EVAL": "1"}):
            self.assertTrue(reference_eval())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_h1_workspace(root)
            settings = research_settings(timeframe="1h", initial_capital_rub=100000.0)
            service = ProofService(root)
            pair_req = pair_screen_request(
                parameters={"lookback": 10, "entry_z": 1.2, "exit_z": 0.3}
            )
            cal_req = calendar_spread_request(
                parameters={"window": 10, "entry_z": 1.2, "exit_z": 0.3}
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MOEX_PROOF_REFERENCE_EVAL", None)
                pair_replay = service.prepare(pair_req, settings=settings)(
                    pair_req, through="2026-01-20"
                )
                self.assertIn("session_date", pair_replay["series"][0])
                self.assertIn("free_cash_rub", pair_replay["ledger"][0])
                self.assertIn("run_id", pair_replay)
                self.assertIn("provenance", pair_replay)

                cal_replay = service.prepare(cal_req, settings=settings)(
                    cal_req, through="2023-03-01"
                )
                self.assertIn("session_date", cal_replay["series"][0])
                self.assertIn("free_cash_rub", cal_replay["ledger"][0])
                self.assertIn("run_id", cal_replay)


if __name__ == "__main__":
    unittest.main()
