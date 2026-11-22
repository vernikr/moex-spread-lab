"""Focused C99 `_fastcore` export inventory and Python-fallback parity tests."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from unittest.mock import patch

from moex_spread_lab.core._fastcore_loader import load_fastcore
from moex_spread_lab.strategies.calendar_ruonia.engine import convergence_trades
from moex_spread_lab.strategies.calendar_zscore.engine import zscore_trades

# The gate compares these keys to FastCoreMethods and verifies that every named
# test exists, so a new C export cannot silently miss the Python parity suite.
FASTCORE_EXPORT_PARITY_TESTS = {
    "compute_rolling_z": "packages/proof/tests/test_perf_equivalence.py::ProofPerfEquivalenceTests.test_fastcore_c_matches_python_fallback_bit_for_bit",
    "calendar_sample_z": "packages/proof/tests/test_fastcore_parity.py::test_calendar_zscore_matches_python_fallback",
    "calendar_zscore_scan": "packages/proof/tests/test_fastcore_parity.py::test_calendar_zscore_matches_python_fallback",
    "calendar_convergence_scan": "packages/proof/tests/test_fastcore_parity.py::test_calendar_convergence_matches_python_fallback",
    "clean_obj": "packages/proof/tests/test_perf_equivalence.py::ProofPerfEquivalenceTests.test_fastcore_c_matches_python_fallback_bit_for_bit",
    "mean_reversion_scan": "packages/proof/tests/test_perf_equivalence.py::ProofPerfEquivalenceTests.test_fastcore_c_matches_python_fallback_bit_for_bit",
    "size_proof_metrics": "packages/proof/tests/test_perf_equivalence.py::ProofPerfEquivalenceTests.test_fastcore_c_matches_python_fallback_bit_for_bit",
}


def _canon(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _compare_native_to_fallback(call):
    assert load_fastcore() is not None, "_fastcore extension must load for native parity tests"
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MOEX_PROOF_DISABLE_C", None)
        native = call()
    with patch.dict(os.environ, {"MOEX_PROOF_DISABLE_C": "1"}, clear=False):
        assert load_fastcore() is None
        fallback = call()
    assert _canon(native) == _canon(fallback)
    return native


def _calendar_rows() -> list[dict[str, object]]:
    spreads = [0.0, 1.0, 3.0, 1.0, -2.0, 0.0, 4.0, 1.0, -3.0, -1.0, 2.0, 5.0]
    deviations = [0.0, 1.5, 2.0, -1.5, -2.0, 0.0, 2.5, -2.5, 1.0, -2.0, 2.0, 0.0]
    return [
        {
            "date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            "spread": spread,
            "open_spread": spread + 0.01,
            "dev_pp": deviation,
            "days_to_near_expiry": 18 - index,
            "dividend_in_window": False,
            "signal_eligible": index != 4,
        }
        for index, (spread, deviation) in enumerate(zip(spreads, deviations, strict=True))
    ]


def test_fastcore_exports_remain_in_parity_inventory():
    fastcore = load_fastcore()
    assert fastcore is not None, (
        "_fastcore extension should build and load when compiler is available"
    )
    missing = [
        name for name in FASTCORE_EXPORT_PARITY_TESTS if not callable(getattr(fastcore, name, None))
    ]
    assert not missing, f"Missing _fastcore exports: {missing}"


def test_calendar_zscore_matches_python_fallback():
    rows = _calendar_rows()
    result = _compare_native_to_fallback(
        lambda: zscore_trades(
            rows,
            delay=1,
            fill="open",
            entry_z=0.7,
            exit_z=0.2,
            window=3,
            max_hold_days=5,
            close_before=2,
            skip_dividend_windows=False,
            close_at_end=True,
        )
    )
    assert result, "fixture should exercise at least one calendar z-score trade"


def test_calendar_convergence_matches_python_fallback():
    rows = [
        {**row, "days_to_near_expiry": 12 - index} for index, row in enumerate(_calendar_rows())
    ]
    result = _compare_native_to_fallback(
        lambda: convergence_trades(
            rows,
            threshold=1.0,
            delay=1,
            fill="open",
            exit_days=3,
            close_at_end=True,
        )
    )
    assert result, "fixture should exercise at least one convergence trade"
