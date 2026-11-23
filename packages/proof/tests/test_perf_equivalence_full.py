"""C99 fastcore fallback tier of the equivalence suite (plan S4a).

The largest full-resolution case: ``_fastcore`` bit-for-bit parity against the pure
Python fallback for rolling z, ``clean_obj``, the mean-reversion baseline, a pair
cohort and a calendar cohort, plus the ``MOEX_PROOF_DISABLE_C`` switch itself.
Kept as its own file behind the ``slow_equiv`` marker; it is named in
``scripts/run_sanitized_pytest.py``, the ASan/UBSan gate and the CI
perf-equivalence job, and no trial count or history is trimmed.
"""

from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.proof_runtime.service import CALENDAR_UNIVERSE, PAIR_UNIVERSE, ProofService
from perf_equivalence_fixtures import (  # noqa: F401  (re-exported helper names)
    _build_h1_workspace,
    _canon,
    _canon_sha,
    _write_h1_candles,
)
from perf_equivalence_pool_fixtures import _build_pool_test_workspace

pytestmark = pytest.mark.slow_equiv


class ProofPerfEquivalenceFastcoreTests(unittest.TestCase):
    """C99 fastcore exports must match the pure-Python fallback bit for bit."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        _build_h1_workspace(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_fastcore_c_matches_python_fallback_bit_for_bit(self):
        from moex_spread_lab.core._fastcore_loader import load_fastcore
        from moex_spread_lab.proof_runtime.service import _clean
        from moex_spread_lab.strategies.pair_mean_reversion.analytics import (
            compute_rolling_z,
            run_mean_reversion_baseline,
        )

        fc = load_fastcore()
        self.assertIsNotNone(
            fc, "_fastcore extension should build and load when compiler is available"
        )

        # 1) compute_rolling_z including constant windows and floating-point sums
        ticks = [float(i % 7 - 3) + 0.125 * (i % 5) for i in range(120)]
        ticks[40:55] = [10.5] * 15
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOEX_PROOF_DISABLE_C", None)
            z_c = compute_rolling_z(ticks, 12, 1)
        with patch.dict(os.environ, {"MOEX_PROOF_DISABLE_C": "1"}, clear=False):
            self.assertIsNone(load_fastcore())
            z_py = compute_rolling_z(ticks, 12, 1)
        self.assertEqual(z_c, z_py)

        # 2) _clean / clean_obj preserving -0.0 and turning non-finite floats into None
        sample = {
            "ok": 1.5,
            "neg_zero": -0.0,
            "bad": [float("nan"), float("inf"), float("-inf"), (1, float("nan"), {"x": -0.0})],
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOEX_PROOF_DISABLE_C", None)
            clean_c = _clean(sample)
        with patch.dict(os.environ, {"MOEX_PROOF_DISABLE_C": "1"}, clear=False):
            clean_py = _clean(sample)
        self.assertEqual(_canon(clean_c), _canon(clean_py))
        self.assertEqual(math.copysign(1.0, clean_c["neg_zero"]), -1.0)

        # 3) run_mean_reversion_baseline(sizing_intents=True, metrics_only=True) with funding + forced exit
        rows = []
        funding = {}
        for i in range(40):
            d = f"2026-01-{i + 1:02d}" if i < 31 else f"2026-02-{i - 30:02d}"
            px = 10.0 + (2.5 if i >= 30 else 0.1 * (i % 4))
            rows.append(
                {
                    "date": d,
                    "timestamp": f"{d}T10:00:00",
                    "near_open": px,
                    "near_close": px + 0.05,
                    "far_open": px + 1.0,
                    "far_close": px + 1.05,
                    "spread_ticks": float((i % 9) - 4 if i < 35 else 20),
                    "benchmark_price": 100.0 + i * 0.05,
                    "benchmark_source_kind": "test",
                }
            )
            funding[d] = 0.002
        series_obj = {
            "near": "CRZ6",
            "far": "CRM6",
            "contracts": {
                "CRZ6": {
                    "initial_margin": 2500.0,
                    "tick_value": 1.0,
                    "tick_size": 1.0,
                    "lot_size": 1.0,
                },
                "CRM6": {
                    "initial_margin": 2500.0,
                    "tick_value": 1.0,
                    "tick_size": 1.0,
                    "lot_size": 1.0,
                },
            },
            "rows": rows,
        }
        z_vals = compute_rolling_z([r["spread_ticks"] for r in rows], 8, 1)
        kw = dict(
            lookback=8,
            entry_z=1.0,
            exit_z=0.3,
            initial_capital=100000.0,
            decision_delay_bars=1,
            analysis_role="diagnostic_only",
            fill="open",
            funding_swaprates=funding,
            sizing_intents=True,
            metrics_only=True,
            precomputed_z=z_vals,
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOEX_PROOF_DISABLE_C", None)
            rep_c = run_mean_reversion_baseline(series_obj, **kw)
        with patch.dict(os.environ, {"MOEX_PROOF_DISABLE_C": "1"}, clear=False):
            rep_py = run_mean_reversion_baseline(series_obj, **kw)
        self.assertEqual(_canon(rep_c), _canon(rep_py))

        # 4) End-to-end pool cohort + plateau + windows with C enabled vs MOEX_PROOF_DISABLE_C=1
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_pool_test_workspace(root)
            targets = [
                {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"},
                {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRM6"},
                {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRU6"},
            ]
            req = {
                "universe": PAIR_UNIVERSE,
                "pair": "CNYRUBF/CRZ6",
                "strategy": "mean_reversion",
                "fill": "next_open",
                "sizing": AUTO_SIZING,
                "optimize_strategy": True,
                "targets": targets,
            }

            def run_with_c(disable_c: str):
                env = {"MOEX_PROOF_WORKERS": "2", "MOEX_PROOF_FORCE_POOL": "1"}
                if disable_c:
                    env["MOEX_PROOF_DISABLE_C"] = disable_c
                with patch.dict(os.environ, env, clear=False):
                    if not disable_c:
                        os.environ.pop("MOEX_PROOF_DISABLE_C", None)
                    svc = ProofService(root)
                    try:
                        return research_run(svc, req, optimize=True)
                    finally:
                        svc.close()

            self.assertEqual(
                _canon(run_with_c("")),
                _canon(run_with_c("1")),
            )

            # 5) Calendar 1h strategy_search (zscore_mean_reversion + convergence_to_ruonia)
            #    C double-double sample stdev/mean + scalar_only sizing vs pure Python fallback
            cal_req = {
                "universe": CALENDAR_UNIVERSE,
                "asset": "SR",
                "pair": "SRH3/SRM3",
                "strategy": "zscore_mean_reversion",
                "fill": "next_open",
                "sizing": AUTO_SIZING,
                "optimize_strategy": True,
                "targets": [
                    {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"},
                    {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRM3/SRU3"},
                ],
            }

            def run_cal(disable_c: str, ref_eval: str = "0"):
                env = {"MOEX_PROOF_WORKERS": "1", "MOEX_PROOF_REFERENCE_EVAL": ref_eval}
                if disable_c:
                    env["MOEX_PROOF_DISABLE_C"] = disable_c
                with patch.dict(os.environ, env, clear=False):
                    if not disable_c:
                        os.environ.pop("MOEX_PROOF_DISABLE_C", None)
                    svc = ProofService(root)
                    try:
                        return research_run(svc, cal_req, optimize=True)
                    finally:
                        svc.close()

            cal_c_fast = _canon(run_cal("", "0"))
            cal_py_fast = _canon(run_cal("1", "0"))
            cal_py_ref = _canon(run_cal("1", "1"))
            self.assertEqual(cal_c_fast, cal_py_fast)
            self.assertEqual(cal_c_fast, cal_py_ref)


if __name__ == "__main__":
    unittest.main()
