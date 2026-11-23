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
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.settings import ResearchSettings, save_settings
from moex_spread_lab.proof_runtime.proof_batch import checked_plateau
from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.proof_runtime.proof_windows import window_summaries
from moex_spread_lab.proof_runtime.service import (
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
        settings = ResearchSettings(
            timeframe="1h",
            validation_percent=20,
            allow_failed_volume=True,
            allow_failed_capital=True,
            initial_capital_rub=100000.0,
            # Fixed seed: these tests compare two runs, which must stay comparable.
            random_seed=41,
        )
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
                {
                    "universe": CALENDAR_UNIVERSE,
                    "asset": "SR",
                    "pair": "SRH3/SRM3",
                    "strategy": "zscore_mean_reversion",
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                    "optimize_strategy": True,
                    "targets": cal_targets,
                },
                optimize=True,
            )
            mixed_opt = research_run(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                    "optimize_strategy": True,
                    "targets": mixed_targets,
                },
                optimize=True,
            )
            plat = checked_plateau(
                service,
                {
                    "universe": CALENDAR_UNIVERSE,
                    "asset": "SR",
                    "pair": "SRH3/SRM3",
                    "strategy": "zscore_mean_reversion",
                    "parameters": {"window": 10, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                    "targets": cal_targets,
                },
            )
            wins = window_summaries(
                service,
                {
                    "requests": [
                        {
                            "universe": CALENDAR_UNIVERSE,
                            "asset": "SR",
                            "pair": t["pair"],
                            "strategy": "convergence_to_ruonia",
                            "parameters": {"threshold_pp": 0.5},
                            "fill": "next_open",
                            "sizing": AUTO_SIZING,
                        }
                        for t in cal_targets
                    ]
                },
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
            settings = ResearchSettings(
                timeframe="1d",
                validation_percent=20,
                allow_failed_volume=True,
                allow_failed_capital=True,
                initial_capital_rub=30000.0,
                # Fixed seed: these tests compare two runs, which must stay comparable.
                random_seed=41,
            )

            def run_for(env_flag: str):
                with (
                    patch.object(ResearchSettings, "load", return_value=settings),
                    patch.dict(os.environ, {"MOEX_PROOF_REFERENCE_EVAL": env_flag}),
                ):
                    svc = ProofService(root)
                    auto_pair = research_run(
                        svc,
                        {
                            "universe": PAIR_UNIVERSE,
                            "pair": "CNYRUBF/CRZ6",
                            "strategy": "mean_reversion",
                            "fill": "next_open",
                            "sizing": AUTO_SIZING,
                            "optimize_strategy": True,
                            "targets": [{"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"}],
                        },
                        optimize=True,
                    )
                    fixed_pair = research_run(
                        svc,
                        {
                            "universe": PAIR_UNIVERSE,
                            "pair": "CNYRUBF/CRZ6",
                            "strategy": "mean_reversion",
                            "fill": "next_open",
                            "quantity": 1,
                            "slippage_ticks": 1.0,
                            "optimize_strategy": False,
                            "targets": [{"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"}],
                        },
                        optimize=True,
                    )
                    cal_1d = research_run(
                        svc,
                        {
                            "universe": CALENDAR_UNIVERSE,
                            "asset": "SR",
                            "pair": "SRH3/SRM3",
                            "strategy": "zscore_mean_reversion",
                            "fill": "next_open",
                            "sizing": AUTO_SIZING,
                            "optimize_strategy": True,
                            "targets": [
                                {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"}
                            ],
                        },
                        optimize=True,
                    )
                    return {"auto_pair": auto_pair, "fixed_pair": fixed_pair, "cal_1d": cal_1d}

            expected = run_for("1")
            actual = run_for("0")
            self.assertEqual(_canon_sha(actual), _canon_sha(expected))
            self.assertEqual(actual, expected)

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
            pair_before = research_run(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                },
                optimize=False,
            )
            cal_before = research_run(
                service,
                {
                    "universe": CALENDAR_UNIVERSE,
                    "asset": "SR",
                    "pair": "SRH3/SRM3",
                    "strategy": "zscore_mean_reversion",
                    "parameters": {"window": 10, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                },
                optimize=False,
            )

            # 2. Mutate CRZ6_1h.json with the EXACT same byte length. A fresh
            #    (path, st_mtime_ns, st_size) key must invalidate the cache.
            crz6_path = root / "data/w1/candles-screen/CRZ6_1h.json"
            raw_crz6 = crz6_path.read_text(encoding="utf-8")
            mutated_crz6 = raw_crz6.replace('"volume": 60', '"volume": 61', 1)
            self.assertEqual(len(raw_crz6), len(mutated_crz6))
            before_stat = crz6_path.stat()
            time.sleep(0.02)  # guarantee a different st_mtime_ns on rewrite
            crz6_path.write_text(mutated_crz6, encoding="utf-8")
            after_stat = crz6_path.stat()
            self.assertNotEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)

            pair_after = research_run(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                },
                optimize=False,
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
            # same-length value so st_size is unchanged but raw bytes (and sha256)
            # differ.
            self.assertIn("30000.0", raw_srh3)
            mutated_srh3 = raw_srh3.replace("30000.0", "30001.0", 1)
            self.assertEqual(len(raw_srh3), len(mutated_srh3))
            before_stat = srh3_eod.stat()
            time.sleep(0.02)  # guarantee a different st_mtime_ns on rewrite
            srh3_eod.write_text(mutated_srh3, encoding="utf-8")
            after_stat = srh3_eod.stat()
            self.assertNotEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)

            cal_after = research_run(
                service,
                {
                    "universe": CALENDAR_UNIVERSE,
                    "asset": "SR",
                    "pair": "SRH3/SRM3",
                    "strategy": "zscore_mean_reversion",
                    "parameters": {"window": 10, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                },
                optimize=False,
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
            till_match = srh3_eod.name.split("-")[4:7]
            till_str = "-".join(till_match)
            before_hist = fetch_history(
                "SRH3",
                HISTORY_FROM,
                till_str,
                cache_dir=root / "data/spread-history-cache",
                delay_seconds=0,
                fields=HISTORY_FIELDS,
            )
            mutated_srh3_2 = mutated_srh3.replace("30001.0", "30002.0", 1)
            self.assertEqual(len(mutated_srh3), len(mutated_srh3_2))
            time.sleep(0.02)
            srh3_eod.write_text(mutated_srh3_2, encoding="utf-8")
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
            settings = ResearchSettings(
                timeframe="1h",
                validation_percent=20,
                allow_failed_volume=True,
                allow_failed_capital=True,
                initial_capital_rub=100000.0,
                # Fixed seed: this test compares two runs, which must stay comparable.
                random_seed=41,
            )
            service = ProofService(root)
            pair_req = {
                "universe": PAIR_UNIVERSE,
                "pair": "CNYRUBF/CRZ6",
                "strategy": "mean_reversion",
                "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                "fill": "next_open",
                "sizing": AUTO_SIZING,
            }
            cal_req = {
                "universe": CALENDAR_UNIVERSE,
                "asset": "SR",
                "pair": "SRH3/SRM3",
                "strategy": "zscore_mean_reversion",
                "parameters": {"window": 10, "entry_z": 1.2, "exit_z": 0.3},
                "fill": "next_open",
                "sizing": AUTO_SIZING,
            }
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
