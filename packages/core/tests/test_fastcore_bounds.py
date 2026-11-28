"""Direct-kernel input-boundary validation for ``_fastcore`` (#293, #294, #295, #296).

The Python wrapper validates everything first; these tests pin the kernel's own
defense-in-depth contract for direct callers (plugins, scripts), mirroring what
``sizing_fast`` enforces upstream.
"""

from __future__ import annotations

from array import array

import pytest

from moex_spread_lab.core import _fastcore_loader as loader

fc = loader.load_fastcore()
pytestmark = pytest.mark.skipif(fc is None, reason="_fastcore extension must load")
assert fc is not None

DATES = [f"2026-01-{day:02d}" for day in range(1, 7)]
ROWS = [{"funding_rub": 10.0, "funding_contracts": 1} for _ in range(6)]
DOUBLE = ["d", [1.0] * 6]


def _metrics(specs, rows=ROWS):
    return fc.size_proof_metrics(
        DATES,
        array("d", [1.0] * 6),
        array("d", [0.0] * 6),
        array("d", [1.0] * 6),
        array("d", [1000.0] * 6),
        array("B", [1] * 6),
        array("B", [1] * 6),
        array("B", [1] * 6),
        rows,
        specs,
        30000.0,
        False,
        False,
        False,
        True,
    )


def _spec(entry_idx, exit_idx, orig_idx=0, margin=2500.0, sign=1):
    return (
        orig_idx,
        entry_idx,
        exit_idx,
        sign,
        False,
        1.0,
        1.0,
        margin,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def test_rows_shorter_than_dates_raises_not_segfault() -> None:
    with pytest.raises(ValueError, match="rows must match dates"):
        _metrics([], rows=ROWS[:1])


def test_rows_non_dict_entry_raises() -> None:
    with pytest.raises(TypeError, match="rows entries must be dicts"):
        _metrics([], rows=[1, 2, 3, 4, 5, 6])


def test_spec_exit_index_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match="index out of range"):
        _metrics([_spec(0, 99)])


def test_spec_unsorted_or_duplicate_entries_raise() -> None:
    with pytest.raises(ValueError, match="sorted by entry_idx"):
        _metrics([_spec(2, 4, 0), _spec(1, 3, 1)])
    with pytest.raises(ValueError, match="sorted by entry_idx"):
        _metrics([_spec(1, 3, 0), _spec(1, 4, 1)])


def test_spec_orig_index_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match="index out of range"):
        _metrics([_spec(0, 2, orig_idx=7)])


def test_nan_margin_and_zero_margin_fail_fast() -> None:
    with pytest.raises(ValueError, match="margin must be finite"):
        _metrics([_spec(0, 2, margin=float("nan"))])
    with pytest.raises(ValueError, match="margin must be finite"):
        _metrics([_spec(0, 2, margin=0.0)])
    with pytest.raises(ValueError, match="margin must be finite"):
        _metrics([_spec(0, 2, margin=-1.0)])


def test_rolling_z_rejects_wrong_buffer_dtype() -> None:
    with pytest.raises(TypeError, match="wrong double buffer"):
        fc.compute_rolling_z(array("q", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]), 3, 0)


def test_raw_bytes_rejected_for_double_buffer() -> None:
    with pytest.raises(TypeError, match="wrong double buffer"):
        fc.compute_rolling_z(b"\x00" * 80, 3, 0)


def test_int32_list_values_do_not_truncate() -> None:
    with pytest.raises(OverflowError, match="int32"):
        fc.calendar_zscore_scan(
            [1.0, 2.0, 3.0],
            [0.0, 1.0, 2.0],
            [3 + 2**32, 1, 1],
            [1, 1, 1],
            [1, 1, 1],
            [1, 1, 1],
            0,
            1.0,
            0.0,
            10.0,
            0,
            True,
        )


def test_non_bool_int32_flag_columns_rejected() -> None:
    with pytest.raises(TypeError, match="byte flags"):
        fc.calendar_zscore_scan(
            [1.0, 2.0, 3.0],
            [0.0, 1.0, 2.0],
            [1, 1, 1],
            [2, 1, 1],  # flags must be 0/1
            [1, 1, 1],
            [1, 1, 1],
            0,
            1.0,
            0.0,
            10.0,
            0,
            True,
        )


@pytest.mark.parametrize(
    "call",
    [
        lambda: fc.calendar_zscore_scan(
            [1.0, 2.0],
            [0.0, 1.0],
            array("i", [30, 30]),
            array("B", [1, 1]),
            array("B", [1, 1]),
            array("B", [1, 1]),
            -1,
            1.0,
            0.0,
            10.0,
            0,
            True,
        ),
        lambda: fc.calendar_convergence_scan(
            [1.0, 2.0],
            array("i", [30, 30]),
            array("B", [1, 1]),
            array("B", [1, 1]),
            array("B", [1, 1]),
            1.0,
            -1,
            5,
            True,
        ),
        lambda: fc.mean_reversion_scan([1.0, 2.0], 2, 5, -1, 1.0, 0.5, False),
        lambda: fc.mean_reversion_scan([1.0, 2.0], 2, 0, 0, 1.0, 0.5, False),
    ],
)
def test_negative_delay_and_lookback_rejected(call) -> None:
    with pytest.raises(ValueError, match=r"delay|lookback"):
        call()


def test_int64_elig_prefix_rejected() -> None:
    with pytest.raises(TypeError, match="wrong int32 buffer"):
        fc.calendar_sample_z(
            array("d", [1.0, 2.0, 3.0, 4.0]),
            array("B", [1, 1, 1, 1]),
            array("q", [0, 1, 2, 3, 4]),
            2,
            False,
        )
