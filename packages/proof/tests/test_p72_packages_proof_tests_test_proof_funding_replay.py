"""Funding replay scenarios split from test_proof_funding (P7.2 test health, #129)."""

import json
from unittest.mock import Mock

from funding_fixtures import PAIR, WEEKEND_PAIR, change, page, request, weekend
from helpers_p72_packages_proof_tests_test_proof_funding import FundingReplayCase
from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime.proof_batch import checked_plateau
from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.strategies.funding_carry.funding_recheck import recheck
from moex_spread_lab.strategies.funding_carry.proof_funding import NO_CLEARING, supplement_path
from moex_spread_lab.web.server import build_result


class FundingReplayTests(FundingReplayCase):
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
                    ledger_row = ledger[row["date"]]
                    self.assertAlmostEqual(
                        ledger_row["funding_rub"],
                        -ledger_row["position"] * 0.02 * 1000 * 2,
                        places=6,
                    )
                    cash += abs(ledger_row["funding_rub"])
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
            self.assertFalse(any(row["position"] for row in r["ledger"][:9]))
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
                ledger_row = next(x for x in r["ledger"] if x["date"] == "2025-08-18")
                self.assertAlmostEqual(
                    ledger_row["funding_rub"], -ledger_row["position"] * 0.015 * 1000 * 2
                )

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
