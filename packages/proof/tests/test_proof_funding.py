import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from funding_fixtures import PAIR, WEEKEND_PAIR, change, fixture, page, request, weekend
from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime.proof_batch import checked_plateau
from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.proof_runtime.service import ProofService
from moex_spread_lab.strategies.funding_carry.carry import fetch_history
from moex_spread_lab.strategies.funding_carry.funding_recheck import recheck
from moex_spread_lab.strategies.funding_carry.proof_funding import (
    NO_CLEARING,
    FundingTimeline,
    published_rates,
    supplement_path,
)
from moex_spread_lab.web.server import ScreenerInputs, _pair, build_result, catalog


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
                t = FundingTimeline("TEST", dict(zip(days, values)), days)
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


class FundingReplayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.days = fixture(self.root)
        self.service = ProofService(self.root)
        self.inputs = ScreenerInputs(self.root)

    def entry(self, pair=PAIR):
        return catalog(self.inputs, registry=(_pair(pair),))["pairs"][0]

    def test_three_estimates_reconcile_in_both_engines_and_are_not_written_to_source(
        self,
    ):
        d = self.days
        change(
            self.root,
            "CNYRUBF",
            {d[19]: 0.005, d[20]: None, d[21]: None, d[22]: None, d[23]: 0.035},
        )
        raw = page(self.root, "CNYRUBF").read_bytes()
        self.assertTrue(all(self.entry()["proof_strategies"].values()))
        for strategy in ("mean_reversion", "funding_only"):
            with self.subTest(strategy=strategy):
                req = request(strategy)
                r = self.service.proof(req)
                self.assertAlmostEqual(r["summary"]["ledger_reconciliation_rub"], 0, delta=0.02)
                self.assertEqual(r["provenance"]["funding_estimated_dates"], d[20:23])
                ledger = {row["date"]: row for row in r["ledger"]}
                cash = 0
                for row in r["series"][20:23]:
                    self.assertEqual(row["funding_rate_kind"], "midpoint")
                    self.assertAlmostEqual(row["funding_swaprate_rub_per_unit"], 0.02)
                    l = ledger[row["date"]]
                    self.assertAlmostEqual(
                        l["funding_rub"], -l["position"] * 0.02 * 1000 * 2, places=6
                    )
                    cash += abs(l["funding_rub"])
                    self.assertEqual(
                        (row["funding_left_date"], row["funding_right_date"]),
                        (d[19], d[23]),
                    )
                self.assertGreater(cash, 0)
                self.assertTrue(any("Расчётных ставок: 3." in note for note in r["notes"]))
                self.assertEqual(self.service.proof(req)["run_id"], r["run_id"])
                self.assertEqual(
                    build_result(self.inputs, req)["provenance"]["funding_estimated_dates"],
                    d[20:23],
                )
        self.assertEqual(raw, page(self.root, "CNYRUBF").read_bytes())
        self.assertFalse(supplement_path(self.root, "CNYRUBF").exists())

    def test_missing_eod_row_is_not_a_permission_to_invent_a_settlement_price(self):
        day = self.days[20]
        change(self.root, "CNYRUBF", {day: None}, remove=True)
        e = self.entry()
        self.assertEqual(e["proof_strategies"], {"mean_reversion": True, "funding_only": False})
        self.assertIn(day, e["data_reasons"]["funding_only"])
        r = self.service.proof(request())
        self.assertEqual(r["series"][20]["funding_rate_kind"], "midpoint")
        with self.assertRaisesRegex(W0Error, "Нет расчётной цены"):
            self.service.proof(request("funding_only"))

    def test_absent_sessions_on_both_near_sources_are_counted_before_far_liquidity_filter(
        self,
    ):
        weekend(self.root)  # Independent FORTS settlement source for the active week.
        days = self.days
        gap = days[10:14]
        kept = gap[0]
        change(self.root, "CNYRUBF", {kept: None})
        for sec in PAIR.split("/"):
            change(self.root, sec, dict.fromkeys(gap[1:]), remove=True)
            path = self.inputs.candles(PAIR, sec)
            doc = json.loads(path.read_text())
            doc["rows"] = [r for r in doc["rows"] if r["begin"][:10] not in gap[1:]]
            path.write_text(json.dumps(doc))
        e = self.entry()
        self.assertFalse(e["proof_strategies"]["mean_reversion"])
        self.assertIn("без ставки: 4", e["data_reasons"]["mean_reversion"])

    def test_cutoff_and_training_tail_never_use_right_anchor_from_holdout(self):
        days = self.days
        change(self.root, "CNYRUBF", {days[40]: None, days[41]: None})
        for strategy in ("mean_reversion", "funding_only"):
            with self.subTest(strategy=strategy):
                req = request(strategy)
                replay = self.service.prepare(req)
                self.assertNotIn(
                    "funding_estimated_dates",
                    replay(req, through=days[39])["provenance"],
                )
                with self.assertRaisesRegex(W0Error, days[42]):
                    replay(req, through=days[41])
                self.assertEqual(
                    replay(req, through=days[42])["provenance"]["funding_estimated_dates"],
                    days[40:42],
                )
                save_settings(self.root, {"validation_percent": 25})
                self.assertTrue(all(self.entry()["proof_strategies"].values()))
                with self.assertRaisesRegex(W0Error, "за границей периода"):
                    research_run(self.service, req, optimize=False, include_plateau=False)
                with self.assertRaisesRegex(W0Error, "за границей периода"):
                    build_result(self.inputs, req)
                target = {"universe": "pair_screen", "pair": PAIR}
                req = req | {"targets": [target]}
                group = checked_plateau(self.service, req)
                self.assertEqual(group["status"], "unavailable")
                self.assertIsNone(group["plateau"])
                r = research_run(self.service, req, optimize=True)
                self.assertEqual(r["status"], "selection_requires_confirmation")
                self.assertEqual(r["trial_count"], 0)
                self.assertEqual(r["excluded"][0]["target"], target)
                self.assertIn(days[42], r["excluded"][0]["reason"])

    def test_same_day_funding_decision_cannot_see_the_incoming_anchor(self):
        days = self.days
        req = request("funding_only")
        entries = []
        for right in (0.09, -0.19):
            change(
                self.root,
                "CNYRUBF",
                dict.fromkeys(days, -0.01) | {days[6]: None, days[7]: None, days[8]: right},
            )
            r = self.service.proof(req)
            entries.append([t["entry"]["date"] for t in r["trades"]])
            self.assertFalse(any(l["position"] for l in r["ledger"][:9]))
            self.assertAlmostEqual(
                r["series"][6]["funding_swaprate_rub_per_unit"], (-0.01 + right) / 2
            )
        self.assertEqual(entries[0][0], days[9])
        self.assertEqual(entries[1], [])

    def test_weekend_candles_stay_in_price_journal_but_no_monday_double_booking_or_synthetic_settles(
        self,
    ):
        weekend(self.root)
        e = self.entry(WEEKEND_PAIR)
        self.assertTrue(all(e["proof_strategies"].values()), e["data_reasons"])
        for strategy in ("mean_reversion", "funding_only"):
            with self.subTest(strategy=strategy):
                r = self.service.proof(request(strategy, WEEKEND_PAIR))
                by = {x["date"]: x for x in r["series"]}
                self.assertEqual(set(r["provenance"]["funding_no_clearing_dates"]), NO_CLEARING)
                self.assertAlmostEqual(r["summary"]["ledger_reconciliation_rub"], 0, delta=0.02)
                for day in NO_CLEARING:
                    if strategy == "funding_only":
                        self.assertNotIn(day, by)
                        continue
                    x = by[day]
                    self.assertIsNone(x["funding_swaprate_rub_per_unit"])
                    self.assertEqual(x["funding_contracts"], 0)
                    self.assertEqual((x["funding_rate_kind"], x["funding_rub"]), ("no_clearing", 0))
                    self.assertEqual(x["benchmark_mark_date"], "2025-08-15")
                monday = by["2025-08-18"]
                self.assertEqual(monday["funding_swaprate_rub_per_unit"], 0.015)
                l = next(x for x in r["ledger"] if x["date"] == "2025-08-18")
                self.assertAlmostEqual(l["funding_rub"], -l["position"] * 0.015 * 1000 * 2)

    def test_offline_replay_prefers_published_or_rechecked_over_estimates_and_never_overwrites_intake(
        self,
    ):
        d = self.days
        change(self.root, "CNYRUBF", {d[21]: None, d[22]: None})
        p = page(self.root, "CNYRUBF")
        raw = p.read_bytes()
        fetched = {
            "secid": "CNYRUBF",
            "source": {"raw_response_sha256": ["ef" * 32]},
            "rows": [
                {"TRADEDATE": d[20], "SWAPRATE": -18},
                {"TRADEDATE": d[21], "SWAPRATE": 0},
                {"TRADEDATE": d[22], "SWAPRATE": None},
            ],
        }
        fetch = Mock(return_value=fetched)
        report = recheck(self.root, "CNYRUBF", d[20], d[22], fetch=fetch)
        self.assertEqual((report["numeric"], report["null"]), (2, 1))
        self.assertFalse(supplement_path(self.root, "CNYRUBF").exists())
        fetch.assert_called_once_with("CNYRUBF", d[20], d[22], cache_dir=None, allow_empty=True)
        recheck(self.root, "CNYRUBF", d[20], d[22], fetch=fetch, apply=True)
        r = self.service.proof(request())
        before, restored, estimated = r["series"][20:23]
        self.assertEqual(before["funding_swaprate_rub_per_unit"], 0.015)
        self.assertEqual(
            (restored["funding_rate_kind"], restored["funding_swaprate_rub_per_unit"]),
            ("rechecked", 0),
        )
        self.assertEqual(estimated["funding_rate_kind"], "midpoint")
        self.assertEqual(estimated["funding_left_rate"], 0)
        self.assertEqual(
            r["provenance"]["funding_recheck_pages"][d[21]]["page_hashes"], ["ef" * 32]
        )
        self.assertEqual(raw, p.read_bytes())
        bad = supplement_path(self.root, "CNYRUBF")
        bad.write_text(json.dumps({"schema": "accidental-file"}))
        self.assertFalse(any(self.entry()["proof_strategies"].values()))
