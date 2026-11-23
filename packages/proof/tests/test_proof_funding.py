"""Funding timeline policy tests (P7.2 test health, #129).

Replay scenarios live in
``test_p72_packages_proof_tests_test_proof_funding_replay`` with shared
setup in ``helpers_p72_packages_proof_tests_test_proof_funding``.
"""

import io
import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.funding_carry.carry import fetch_history
from moex_spread_lab.strategies.funding_carry.funding_recheck import recheck
from moex_spread_lab.strategies.funding_carry.proof_funding import (
    NO_CLEARING,
    FundingTimeline,
    published_rates,
    supplement_path,
)


class FundingPolicyTests(unittest.TestCase):
    def test_constant_midpoint_1_2_3_and_observed_zero_both_signs(self):
        dates = ["2025-08-" + f"{d:02d}" for d in (4, 5, 6, 7, 8)]
        for n in (1, 2, 3):
            for a, b in ((0, -0.06), (-0.08, 0.2), (0.08, -0.08)):
                with self.subTest(n=n, endpoints=(a, b)):
                    rates = dict.fromkeys(dates[: n + 2])
                    rates[dates[0]] = a
                    rates[dates[n + 1]] = b
                    t = FundingTimeline("TEST", rates, dates[: n + 2])
                    marks = t.require(rates)
                    self.assertEqual(marks[dates[0]].kind, "published")
                    for d in dates[1 : n + 1]:
                        m = marks[d]
                        self.assertAlmostEqual(m.value, (a + b) / 2)
                        self.assertEqual(m.kind, "midpoint")
                        self.assertEqual((m.left, m.right), (dates[0], dates[n + 1]))
                        self.assertEqual(m.series_fields(t.marks)["funding_left_rate"], a)

    def test_sparse_far_does_not_shorten_four_missing_sessions_or_create_endpoints(
        self,
    ):
        days = ["2025-07-" + f"{i:02d}" for i in (7, 8, 9, 10, 11, 14)]
        for values, asked, message in (
            ([1, None, None, None, None, 2], days[2:3] + days[-1:], "без ставки: 4"),
            ([None, 0, 1, 2, 3, 4], days, "соседних ставок"),
            ([0, 1, 2, 3, 4, None], days, "соседних ставок"),
        ):
            with self.subTest(values=values):
                t = FundingTimeline("TEST", dict(zip(days, values, strict=True)), days)
                with self.assertRaisesRegex(W0Error, message):
                    t.require(asked)

    def test_absent_row_needs_clearing_evidence_currency_regime_and_malformed_values(
        self,
    ):
        a, d, b = "2025-06-06", "2025-06-09", "2025-06-10"
        p = {a: -1, b: 1}
        ds = (a, d, b)
        with self.assertRaisesRegex(W0Error, "не подтверждён отдельный клиринг"):
            FundingTimeline("X", p, ds).require(ds)
        self.assertEqual(FundingTimeline("X", p, ds, clearing_days=[d]).require(ds)[d].value, 0)
        old = {"2022-12-01": 0, "2022-12-02": None, "2022-12-05": 0.25}
        with self.assertRaisesRegex(W0Error, "изменился порядок"):
            FundingTimeline("USDRUBF", old, old).require(old)
        for bad in (True, float("nan"), float("inf"), "bad"):
            with self.subTest(value=bad), self.assertRaises(W0Error):
                published_rates("X", [{"TRADEDATE": a, "SWAPRATE": bad}])
        with self.assertRaisesRegex(W0Error, "ожидалась история"):
            published_rates("X", [{"SECID": "Y", "TRADEDATE": a, "SWAPRATE": 1}])

    def test_empty_official_result_is_not_a_zero_and_partial_cursor_is_not_a_short_gap(
        self,
    ):
        payload = {
            "history": {"columns": ["TRADEDATE", "SWAPRATE"], "data": []},
            "history.cursor": {"columns": ["TOTAL"], "data": [[0]]},
        }

        def open_page(*args, **kwargs):
            return io.BytesIO(json.dumps(payload).encode())

        def fetch(secid, a, b, **kwargs):
            return fetch_history(secid, a, b, opener=open_page, delay_seconds=0, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            r = recheck(Path(tmp), "GAZPF", "2025-08-16", "2025-08-17", fetch=fetch)
            self.assertEqual((r["eod_rows"], r["numeric"], r["first_numeric"]), (0, 0, None))
            self.assertTrue(r["page_hashes"])
            payload["history.cursor"]["data"] = [[1]]
            with self.assertRaisesRegex(W0Error, "Неполная история"):
                recheck(
                    Path(tmp),
                    "GAZPF",
                    "2025-08-16",
                    "2025-08-17",
                    fetch=fetch,
                    apply=True,
                )
            self.assertFalse(supplement_path(Path(tmp), "GAZPF").exists())

    def test_confirmed_nonclearing_is_not_a_published_zero_or_an_interpolation_anchor(
        self,
    ):
        a, d, b = "2025-08-15", "2025-08-18", "2025-08-19"
        p = {a: 0, d: None, b: -0.04}
        ds = sorted(set(p) | NO_CLEARING)
        t = FundingTimeline("GAZPF", p, ds)
        m = t.require(ds)
        self.assertEqual((m[a].kind, m[a].value), ("published", 0))
        self.assertEqual(m[d].value, -0.02)
        self.assertEqual((m[d].left, m[d].right), (a, b))
        for weekend_date in NO_CLEARING:
            self.assertEqual((m[weekend_date].kind, m[weekend_date].value), ("no_clearing", None))
        with self.assertRaisesRegex(W0Error, "противоречит"):
            FundingTimeline("GAZPF", p | {"2025-08-16": 0.12}, ds)
        with self.assertRaises(W0Error):
            FundingTimeline("CNYRUBF", p, ds).require(ds)
