"""Reusable band, fill and compact-table expectations for the proof-window tests.

Extracted from ``test_proof_windows.py`` by P7.2 test-health leaf #132 so the
test module stays inside the file-size and cognitive-complexity limits. Every
helper keeps the original assertion set; the tests only orchestrate them.
"""

import statistics
from copy import deepcopy
from typing import Any

from moex_spread_lab.strategies.signals import attach_entry_levels
from proof_windows_fixtures_p72_packages_proof_tests_test_proof_windows import FixtureCase


def band_levels(
    base: dict[str, Any], fill: str, series: list[dict[str, Any]]
) -> tuple[dict[str, Any], float, float]:
    """Return the first full-window row and the entry/exit bands it must carry.

    The pair universe prices a RUB-neutral spread, so it uses the population
    deviation of the raw spread; the calendar universe prices a RUB point value,
    so it uses the sample deviation of the RUB-scaled spread. ``reference_same_bar``
    consumes one extra leading row because entry happens on the signal bar.
    """
    native = base["universe"] == "calendar_native"
    rows = [r for r in series if r.get("volume_eligible", True)]
    window = base["parameters"]["window" if native else "lookback"]
    i = window + 1
    end = i + 1 if native and fill == "reference_same_bar" else i
    sample = [
        r["spread"] * (r["point_value_rub"] if native else 1) for r in rows[end - window : end]
    ]
    mean = statistics.mean(sample)
    deviation = statistics.stdev(sample) if native else statistics.pstdev(sample)
    point = rows[i]["point_value_rub"] if native else 1
    entry_z = base["parameters"]["entry_z"]
    return rows[i], (mean - entry_z * deviation) / point, (mean + entry_z * deviation) / point


def assert_fill_selection(case: FixtureCase, proof: dict[str, Any], fill: str) -> None:
    """The selected fill drives execution reference, replay identity and entry prices."""
    same_bar = fill == "reference_same_bar"
    case.assertEqual(proof["selection"]["fill"], fill)
    case.assertEqual(proof["execution_reference"], same_bar)
    case.assertEqual(proof["evaluation"]["execution_reference"], same_bar)
    case.assertEqual(proof["summary"]["ledger_reconciliation_rub"], 0)
    case.assertEqual(case.run_proof(proof["selection"])["run_id"], proof["run_id"])
    case.assertTrue(proof["trades"])
    rows = {r["date"]: r for r in proof["series"]}
    for trade in proof["trades"]:
        row = rows[trade["entry"]["date"]]
        case.assertEqual(
            trade["entry"]["near_price"],
            row["near_open" if fill == "next_open" else "near"],
        )
        if same_bar:
            case.assertEqual(trade["signal"]["date"], trade["entry"]["date"])
    if same_bar:
        case.assertTrue(any("не является проверкой исполнимой" in note for note in proof["notes"]))


def assert_entry_matches_signal_band(
    case: FixtureCase, trade: dict[str, Any], signal: dict[str, Any]
) -> None:
    """A long entry triggers at or below the buy band, a short one at or above the sell."""
    long_side = trade["side"] == "long_spread"
    bound = signal["buy_level" if long_side else "sell_level"]
    case.assertIsNotNone(bound)
    if long_side:
        case.assertLessEqual(signal["spread"] - bound, 1e-6)
    else:
        case.assertGreaterEqual(signal["spread"] - bound, -1e-6)


def assert_zero_volume_row_is_ignored(case: FixtureCase, proof: dict[str, Any]) -> None:
    """A zero-volume row cannot poison later bands, even with an absurd spread."""
    altered = deepcopy(proof)
    altered["series"][0]["volume_eligible"] = False
    altered["series"][0]["spread"] = 1e10
    attach_entry_levels(altered)
    without = deepcopy(proof)
    without["series"] = without["series"][1:]
    attach_entry_levels(without)
    case.assertEqual(
        [(r["buy_level"], r["sell_level"]) for r in altered["series"]][1:],
        [(r["buy_level"], r["sell_level"]) for r in without["series"]],
    )


def assert_bands_are_prefix_causal(case: FixtureCase, proof: dict[str, Any]) -> None:
    """Bands recomputed on a truncated series reproduce the full-series prefix."""
    prefix = deepcopy(proof)
    prefix["series"] = prefix["series"][:30]
    attach_entry_levels(prefix)
    case.assertEqual(
        [(r["buy_level"], r["sell_level"]) for r in proof["series"]][:30],
        [(r["buy_level"], r["sell_level"]) for r in prefix["series"]],
    )


def assert_bands_follow_point_value(case: FixtureCase, proof: dict[str, Any]) -> None:
    """Changing the RUB point value moves the bands, so it is not ignored."""
    fx = deepcopy(proof)
    for i, row in enumerate(fx["series"]):
        row["point_value_rub"] = 2 + i / 10
    attach_entry_levels(fx)
    case.assertNotEqual(fx["series"][20]["buy_level"], proof["series"][20]["buy_level"])


def assert_compact_row_matches_proof(
    case: FixtureCase, row: dict[str, Any], proof: dict[str, Any]
) -> None:
    """A compact window row is the exact chart result with the heavy payloads omitted."""
    case.assertEqual(row["status"], "measured")
    case.assertEqual(row["run_id"], proof["run_id"])
    case.assertEqual(row["trades"], proof["summary"]["trades"])
    case.assertEqual(
        row["annualized_excess_pp"],
        proof["evaluation"]["result"].get("annualized_excess_pp"),
    )
    case.assertEqual(row["evaluation"], proof["evaluation"])
    case.assertNotIn("ledger", row)
    case.assertNotIn("plateau", row)
