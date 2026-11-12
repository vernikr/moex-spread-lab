import http.client
import json
import math
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from moex_spread_lab.calendar_spreads import CalendarData, HISTORY_FIELDS, HISTORY_FROM
from moex_spread_lab.carry import _history_url
from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler
from moex_spread_lab.w0 import W0Error

TILL = "2023-06-15"


def _weekdays(start: date, end: date) -> list[str]:
    out, day = [], start
    while day <= end:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def _write_history(root: Path, secid: str, rows: list[list]) -> None:
    url = _history_url(secid, HISTORY_FROM, TILL, 0, HISTORY_FIELDS)
    payload = {
        "history": {"columns": list(HISTORY_FIELDS), "data": rows},
        "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, len(rows), 100]]},
    }
    path = root / "data/spread-history-cache" / f"{secid}-{HISTORY_FROM}-{TILL}-0.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "payload": payload, "raw_response_sha256": "x"}), encoding="utf-8")


def _build_root(root: Path) -> None:
    near_days = _weekdays(date(2022, 12, 12), date(2023, 3, 16))
    far_days = _weekdays(date(2022, 12, 12), date(2023, 6, 15))
    far_price = {d: 31000 + 40 * math.sin(i / 2) + (350 if i == 30 else 0) for i, d in enumerate(far_days)}
    row = lambda secid, d, settle, volume, opening: [d, secid, opening, settle + 5, settle - 5, settle, settle, volume, 20]
    _write_history(root, "SRH3", [row("SRH3", d, 30000.0, 9000, 29990.0) for d in near_days])
    _write_history(root, "SRM3", [row("SRM3", d, far_price[d], 3000, far_price[d] + 8) for d in far_days])
    _write_history(root, "SRH3SRM3", [row("SRH3SRM3", d, far_price[d] - 30000.0, 400, 0.0) for d in near_days])
    (root / "data/w1").mkdir(parents=True, exist_ok=True)
    (root / "data/w1/current-intake.json").write_text(json.dumps({"instruments": [{
        "secid": "SRZ6", "expiry": "2026-12-17", "tick_size": 1.0, "tick_value": 1.0, "initial_margin": 3000.0, "settlement": 30000.0,
    }]}), encoding="utf-8")
    (root / "data/benchmark-cache").mkdir(parents=True, exist_ok=True)
    (root / "data/benchmark-cache/CBR_RUONIA.json").write_text(json.dumps({"rows": [{"date": "2022-01-01", "rate": 15.0}]}), encoding="utf-8")
    (root / "data/benchmark").mkdir(parents=True, exist_ok=True)
    (root / "data/benchmark/lqdt-lineage.json").write_text(json.dumps({"rows": [
        {"date": d, "price": 1.0 + 0.0004 * i} for i, d in enumerate(far_days)]}), encoding="utf-8")


class CalendarProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        _build_root(self.root)
        self.data = CalendarData(self.root)

    def test_catalog_lists_window_with_volume_and_capital_flags(self):
        catalog = self.data.catalog()
        self.assertEqual(catalog["till"], TILL)
        asset = next(a for a in catalog["assets"] if a["asset"] == "SR")
        window = asset["windows"][0]
        self.assertEqual(window["pair"], "SRH3/SRM3")
        self.assertTrue(window["tradable_every_day"])
        self.assertTrue(window["passes_capital_screen"])

    def test_proof_reconciles_ledger_to_trades_and_is_strict_json(self):
        proof = self.data.proof({"asset": "SR", "pair": "SRH3/SRM3", "strategy": "zscore_mean_reversion", "fill": "next_open",
                                 "quantity": 2, "slippage_ticks": 1.0, "parameters": {"entry_z": 1.5, "exit_z": 0.5, "window": 20}})
        json.dumps(proof, allow_nan=False)
        self.assertGreaterEqual(len(proof["trades"]), 1)
        self.assertEqual(proof["summary"]["ledger_reconciliation_rub"], 0.0)
        self.assertEqual(proof["summary"]["fx_point_value_drift_rub"], 0.0)
        first = proof["trades"][0]
        self.assertEqual(first["quantity"], 2)
        self.assertGreater(first["entry"]["costs"]["exchange_clearing"], 0)
        self.assertEqual(first["entry"]["spread_volume"], 400)
        self.assertEqual(first["net_rub"], round(first["gross_rub"] - first["costs_rub"], 2))
        self.assertEqual(len(proof["series"]), len(proof["ledger"]))
        self.assertAlmostEqual(proof["ledger"][-1]["cumulative_net_rub"], proof["summary"]["net_rub"], places=1)

    def test_invalid_selections_are_rejected(self):
        base = {"asset": "SR", "pair": "SRH3/SRM3"}
        for bad in ({**base, "quantity": 0}, {**base, "asset": "XX"}, {**base, "pair": "SRM3/SRU3"}, {**base, "strategy": "x"},
                    {**base, "parameters": {"unknown": 1}}, {**base, "parameters": {"entry_z": -1}}):
            with self.assertRaises(W0Error):
                self.data.proof(bad)

    def test_http_routes_serve_page_catalog_and_proof(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(self.root))))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/proof")
        page = connection.getresponse()
        self.assertEqual(page.status, 200)
        self.assertIn(b"Execution proof", page.read())
        connection.request("GET", "/api/proof/catalog")
        catalog = connection.getresponse()
        self.assertEqual(catalog.status, 200)
        catalog.read()
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server.server_port}"}
        connection.request("POST", "/api/proof/run", json.dumps({"asset": "SR", "pair": "SRH3/SRM3"}), headers)
        run = connection.getresponse()
        self.assertEqual(run.status, 200)
        self.assertEqual(json.loads(run.read())["selection"]["pair"], "SRH3/SRM3")
        connection.request("POST", "/api/proof/run", json.dumps({"asset": "SR", "pair": "nope"}), headers)
        bad = connection.getresponse()
        self.assertEqual(bad.status, 400)
        bad.read()
        connection.close()


if __name__ == "__main__":
    unittest.main()
