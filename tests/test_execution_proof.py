import http.client
import json
import math
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from moex_spread_lab.carry import HISTORY_FIELDS, _history_url
from moex_spread_lab.proof import ProofService
from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler
from moex_spread_lab.w0 import W0Error

START, END = "2025-06-17", "2026-10-27"


def _weekdays(start: date, count: int) -> list[date]:
    out, day = [], start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _near_close(i: int) -> float:
    return 12 + 0.9 * math.sin(i * 0.55) + i * 0.012


def _far_close(i: int) -> float:
    return 13.1 + 0.9 * math.sin(i * 0.55 + 0.4) + i * 0.012


def _write_carry_history(root: Path, secid: str, days: list[date], close) -> None:
    data = []
    for i, d in enumerate(days):
        settle = close(i)
        data.append(["RFUD", d.isoformat(), secid, round(settle, 4), round(settle, 4), 0.015, 100000,
                     500000.0, 5000, round(settle, 4), 250, "CNY"])
    path = root / "data" / "carry-history-cache" / f"{secid}-{START}-{END}-0.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    url = _history_url(secid, START, END, 0, HISTORY_FIELDS)
    payload = {"history": {"columns": list(HISTORY_FIELDS), "data": data},
               "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, len(data), 100]]}}
    path.write_text(json.dumps({"url": url, "payload": payload, "raw_response_sha256": "ab" * 32}), encoding="utf-8")


def _build_root(root: Path) -> list[date]:
    days = _weekdays(date(2025, 6, 17), 55)
    (root / "data/w1/candles").mkdir(parents=True)
    (root / "data/benchmark").mkdir(parents=True)
    (root / "data/w1").mkdir(parents=True, exist_ok=True)
    (root / "data/w1/current-intake.json").write_text(json.dumps({"schema_version": "iss-intake.v1", "instruments": [
        {"secid": secid, "stable_id": secid, "board": "RFUD", "expiry": "2026-12-17", "lot_size": 1000,
         "tick_size": 0.001, "tick_value": 1, "initial_margin": 1000, "source_vintage": "current_snapshot"}
        for secid in ("CNYRUBF", "CRZ6")
    ]}), encoding="utf-8")
    (root / "data/benchmark/lqdt-lineage.json").write_text(json.dumps({
        "schema_version": "lqdt-lineage.v1",
        "rows": [{"date": day.isoformat(), "price": 1 + i * 0.0004, "source_kind": "observed", "source_ticker": "LQDT"}
                 for i, day in enumerate(days)]}), encoding="utf-8")
    for secid, close in (("CNYRUBF", _near_close), ("CRZ6", _far_close)):
        rows = []
        for i, day in enumerate(days):
            value = close(i)
            rows.append({"begin": f"{day.isoformat()} 00:00:00", "open": round(value - 0.06, 4),
                         "close": round(value, 4), "high": round(value + 0.1, 4), "low": round(value - 0.1, 4),
                         "volume": 5000, "price_proxy_kind": "candle_close"})
        (root / f"data/w1/candles/{secid}_1d.json").write_text(json.dumps(
            {"schema_version": "iss-candles.v1", "secid": secid, "interval": 24, "rows": rows}), encoding="utf-8")
    _write_carry_history(root, "CNYRUBF", days, _near_close)
    _write_carry_history(root, "CRZ6", days, _far_close)
    return days


class ExecutionProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        _build_root(self.root)
        self.proof = ProofService(self.root)

    def test_catalog_lists_both_universes_and_pair_availability(self):
        catalog = self.proof.catalog()
        self.assertIn("pair_screen", catalog)
        self.assertIn("universes", catalog)
        pair = next(item for item in catalog["pair_screen"]["pairs"] if item["id"] == "CNYRUBF/CRZ6")
        self.assertEqual(pair["strategies"], {"mean_reversion": True, "funding_only": True})
        # calendar block degrades gracefully when the calendar cache is absent
        self.assertIn("assets", catalog)

    def test_mean_reversion_proof_reconciles(self):
        result = self.proof.proof({"universe": "pair_screen", "pair": "CNYRUBF/CRZ6",
                                   "strategy": "mean_reversion", "quantity": 2, "slippage_ticks": 1.0,
                                   "parameters": {"entry_z": 1.5, "exit_z": 0.3, "lookback": 20}})
        json.dumps(result, allow_nan=False)
        self.assertEqual(result["schema_version"], "execution-proof.v1")
        self.assertEqual(result["universe"], "pair_screen")
        self.assertGreaterEqual(len(result["trades"]), 1)
        summary = result["summary"]
        self.assertEqual(summary["ledger_reconciliation_rub"], 0.0)
        self.assertEqual(summary["fx_point_value_drift_rub"], None)
        self.assertEqual(summary["cost_totals"]["broker"], 0.0)
        ledger_costs = round(sum(row["costs_rub"] for row in result["ledger"]), 2)
        self.assertAlmostEqual(ledger_costs, round(summary["costs_rub"], 2), delta=0.02)
        self.assertAlmostEqual(result["ledger"][-1]["cumulative_net_rub"], summary["net_rub"], delta=0.02)
        self.assertEqual(summary["closed_trades"] + summary["open_positions"], summary["trades"])
        components = sum(sum((trade["entry"]["costs"] or {}).values()) + sum((trade["exit"]["costs"] or {}).values())
                         for trade in result["trades"])
        self.assertAlmostEqual(components, summary["costs_rub"], delta=0.02)
        first = result["trades"][0]
        self.assertEqual(first["entry"]["near_volume"], 5000)
        self.assertGreater(first["entry"]["costs"]["exchange_clearing"], 0)
        self.assertEqual(first["signal"]["kind"], "z")

    def test_funding_only_proof_reconciles_and_flags_open_position(self):
        result = self.proof.proof({"universe": "pair_screen", "pair": "CNYRUBF/CRZ6",
                                   "strategy": "funding_only", "quantity": 2, "slippage_ticks": 1.0})
        json.dumps(result, allow_nan=False)
        self.assertGreaterEqual(len(result["trades"]), 1)
        summary = result["summary"]
        self.assertEqual(summary["ledger_reconciliation_rub"], 0.0)
        self.assertEqual(summary["open_positions"], 1)
        trade = result["trades"][0]
        self.assertTrue(trade["open_at_end"])
        self.assertEqual(trade["side"], "short_spread")
        self.assertEqual(trade["signal"]["reason"], "positive_trailing_funding")
        self.assertEqual(trade["exit"]["costs"], {"exchange_clearing": 0.0, "broker": 0.0, "slippage": 0.0})
        funding_rows = [row for row in result["series"] if (row["funding_rub"] or 0) != 0]
        self.assertTrue(funding_rows)
        funding_ledger = sum(row["funding_rub"] or 0.0 for row in result["ledger"])
        self.assertGreater(funding_ledger, 0.0)
        self.assertAlmostEqual(result["ledger"][-1]["cumulative_net_rub"], summary["net_rub"], delta=0.02)

    def test_deterministic_run_id_and_parameter_sensitivity(self):
        base = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion",
                "quantity": 1, "slippage_ticks": 1.0,
                "parameters": {"entry_z": 1.5, "exit_z": 0.3, "lookback": 20}}
        first = self.proof.proof(base)
        self.assertEqual(first["run_id"], self.proof.proof(base)["run_id"])
        other = self.proof.proof({**base, "slippage_ticks": 2.0})
        self.assertNotEqual(first["run_id"], other["run_id"])

    def test_unknown_universe_and_invalid_selection_rejected(self):
        with self.assertRaises(W0Error):
            self.proof.proof({"universe": "moon"})
        with self.assertRaises(W0Error):
            self.proof.proof({"universe": "pair_screen", "pair": "NOPE", "strategy": "mean_reversion"})
        with self.assertRaises(W0Error):
            self.proof.proof({"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "moon"})
        with self.assertRaises(W0Error):
            self.proof.proof({"universe": "pair_screen", "pair": "CNYRUBF/CRZ6",
                              "strategy": "mean_reversion", "quantity": 0})

    def test_http_routes_pair_proof_vendor_and_legacy_default(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(self.root))))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/vendor/lightweight-charts.standalone.production.js")
        vendor = connection.getresponse()
        self.assertEqual(vendor.status, 200)
        self.assertEqual(vendor.getheader("Content-Type"), "application/javascript; charset=utf-8")
        self.assertIn(b"LightweightCharts", vendor.read())
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server.server_port}"}
        connection.request("POST", "/api/proof/run",
                           json.dumps({"universe": "pair_screen", "pair": "CNYRUBF/CRZ6",
                                        "strategy": "mean_reversion",
                                        "parameters": {"entry_z": 1.5, "exit_z": 0.3, "lookback": 20}}), headers)
        run = connection.getresponse()
        self.assertEqual(run.status, 200)
        self.assertEqual(json.loads(run.read())["universe"], "pair_screen")
        connection.request("POST", "/api/proof/run", json.dumps({"universe": "moon"}), headers)
        bad = connection.getresponse()
        self.assertEqual(bad.status, 400)
        bad.read()
        connection.request("POST", "/api/proof/run", json.dumps({"asset": "SR", "pair": "nope"}), headers)
        bad = connection.getresponse()
        self.assertEqual(bad.status, 400)
        bad.read()
        connection.request("GET", "/api/proof/catalog")
        catalog = connection.getresponse()
        self.assertEqual(catalog.status, 200)
        body = json.loads(catalog.read())
        self.assertIn("pair_screen", body)
        connection.close()


if __name__ == "__main__":
    unittest.main()
