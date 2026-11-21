"""The live checked-window diagnostics must profile the mean, not a selected row or mean of colors."""

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from statistics import fmean
from unittest.mock import patch

from proof_fixtures import fixture
from test_proof_batch import ACTIVE, C, P

from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.proof_runtime.service import ProofService
from moex_spread_lab.proof_runtime.proof_batch import checked_plateau
from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.web.server import ScreenerInputs, ScreenerStore, make_handler
from moex_spread_lab.core.w0 import W0Error


class CheckedPlateauTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        fixture(self.root)
        self.service = ProofService(self.root)
        self.req = {
            "strategy": ACTIVE["strategy"],
            "parameters": ACTIVE["parameters"],
            "targets": [C, P],
        }
        save_settings(self.root, {"validation_percent": 25})

    def profile(self, **kw):
        return checked_plateau(self.service, {**self.req, **kw})

    def test_mean_at_center_and_every_neighbour_precedes_the_stability_ratio(self):
        reqs = [
            {
                **C,
                "strategy": "zscore_mean_reversion",
                "parameters": {"window": 10, "entry_z": 1.5, "exit_z": 0.5},
            },
            ACTIVE,
        ]
        solo = [
            research_run(self.service, r, optimize=False)["plateau"]["fields"]
            for r in reqs
        ]
        # Probes must not start a study, evaluate the holdout or write a trial log.
        with (
            patch(
                "moex_spread_lab.proof_runtime.proof_batch._tune",
                side_effect=AssertionError("not a search"),
            ),
            patch(
                "moex_spread_lab.proof_runtime.proof_search.StudyWindow.report",
                side_effect=AssertionError("not a validation report"),
            ),
        ):
            all_ = self.profile()
        self.assertEqual(
            (all_["status"], all_["member_count"], all_["validation_percent"]),
            ("measured", 2, 25),
        )
        p = all_["plateau"]
        self.assertEqual(p["basis"], "training")
        differs = False
        for name, field in p["fields"].items():
            fs = [solo[0]["window" if name == "lookback" else name], solo[1][name]]
            center = fmean(f["center"]["rank_score"] for f in fs)
            self.assertAlmostEqual(field["center"]["utility"], center)
            expected = []
            for n in field["neighbors"]:
                rs = [
                    next(x for x in f["neighbors"] if x["value"] == n["value"])
                    for f in fs
                ]
                vals = [r.get("rank_score") for r in rs]
                if None in vals:
                    self.assertIsNone(n["utility"])
                    self.assertIn("reason", n)
                    continue
                self.assertEqual(n["by_window_pp"], vals)
                avg = fmean(vals)
                self.assertAlmostEqual(n["rank_score"], avg)
                if any(r["utility"] is not None for r in rs):
                    expected.append(avg)
                    self.assertAlmostEqual(n["utility"], avg)
            score = None
            if expected:
                score = (
                    0
                    if len(expected) != len(field["neighbors"]) or center <= 0
                    else round(100 * max(0, min(1, min(expected) / center)), 2)
                )
            self.assertEqual(field["score"], score)
            if score is not None and all(f["score"] is not None for f in fs):
                differs |= abs(score - fmean(f["score"] for f in fs)) > 0.1
        self.assertTrue(
            differs, "fixture must detect an average-of-individual-lamps implementation"
        )
        self.assertFalse((self.root / "data/analytics/proof-searches.jsonl").exists())

    def test_no_focus_order_or_holdout_leak_and_exact_winner_profile(self):
        save_settings(self.root, {"allow_failed_volume": False})
        before = self.profile()
        self.assertEqual(before, self.profile(**P))
        self.assertEqual(before, self.profile(**C, targets=[P, C]))
        opt = research_run(self.service, self.req, optimize=True)
        self.assertEqual(
            self.profile(parameters=opt["selected_parameters"])["plateau"],
            opt["plateau"],
        )
        path = self.root / "data/w1/candles/CRZ6_1d.json"
        data = json.loads(path.read_text())
        data["rows"][-2]["volume"] = 1
        data["rows"][-2]["close"] += 3
        path.write_text(json.dumps(data))
        self.assertEqual(self.profile(), before)
        alone = self.profile(targets=[P], **C)
        self.assertEqual(alone["member_count"], 1)
        self.assertEqual(
            alone["plateau"],
            research_run(self.service, ACTIVE, optimize=False)["plateau"],
        )
        save_settings(self.root, {"validation_percent": 0})
        blocked = self.profile()
        self.assertEqual(blocked["status"], "unavailable")
        self.assertIsNone(blocked["plateau"])
        self.assertEqual(blocked["excluded"][0]["target"], P)

    def test_incompatible_or_invalid_center_is_not_profiled_on_a_subset_or_clamped(
        self,
    ):
        bad = self.profile(strategy="funding_only", parameters={"lookback": 10})
        self.assertEqual(bad["member_count"], 2)
        self.assertIsNone(bad["plateau"])
        self.assertEqual([x["target"] for x in bad["excluded"]], [C])
        self.assertNotIn("selected_parameters", bad)
        self.assertIsNone(
            self.profile(
                strategy="funding_only", parameters={"lookback": 10}, targets=[C]
            )["plateau"]
        )
        wide = self.profile(parameters={**ACTIVE["parameters"], "lookback": 70})
        fields = wide["plateau"]["fields"]
        self.assertEqual(fields["lookback"]["value"], 70)
        self.assertTrue(
            all(
                f["score"] is None and "диапазон" in f["reason"]
                for f in fields.values()
            )
        )
        for bad_req in (
            {"targets": []},
            {"targets": [C, C]},
            {"parameters": {"entry_z": 1.5}},
            {"parameters": {**ACTIVE["parameters"], "exit_z": 3}},
        ):
            with self.subTest(bad_req=bad_req), self.assertRaises(W0Error):
                self.profile(**bad_req)
        with self.assertRaises(ConnectionAbortedError):
            checked_plateau(
                self.service,
                self.req,
                check_cancelled=lambda: (_ for _ in ()).throw(ConnectionAbortedError()),
            )

    def test_endpoint_accepts_whole_catalog_sized_bodies_and_is_read_only_loopback(
        self,
    ):
        store = ScreenerStore(ScreenerInputs(self.root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        self.addCleanup(conn.close)

        def post(body, extra=None):
            conn.request(
                "POST",
                "/api/proof/plateau",
                body,
                {"Content-Type": "application/json", **(extra or {})},
            )
            r = conn.getresponse()
            return r.status, json.load(r)

        body = json.dumps(self.req) + " " * 9000
        status, result = post(body)
        self.assertEqual(status, 200)
        self.assertEqual(result, self.profile())
        self.assertEqual(post(body, {"Origin": "https://example.org"})[0], 403)
        self.assertEqual(post(body + " " * (192 * 1024))[0], 400)
        self.assertFalse((self.root / "data/analytics/proof-searches.jsonl").exists())
        self.assertFalse(store.proof_profile_lock.locked())
