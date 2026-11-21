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

    def test_hourly_pair_proof_uses_unique_timestamps_and_prior_daily_benchmark(self):
        from moex_spread_lab.evaluation_periods import ResearchSettings, save_settings

        for secid, close_fn in (("CNYRUBF", _near_close), ("CRZ6", _far_close)):
            daily = json.loads((self.root / f"data/w1/candles/{secid}_1d.json").read_text())
            hourly_rows = []
            for i, day_row in enumerate(daily["rows"]):
                day = day_row["begin"][:10]
                base = close_fn(i)
                for j, hour in enumerate((9, 10, 11, 12)):
                    close = base + 0.28 * math.sin(i * 0.65 + j * 1.1 + (secid == "CRZ6"))
                    hourly_rows.append({
                        "begin": f"{day} {hour:02}:00:00",
                        "open": close - 0.025,
                        "high": close + 0.04,
                        "low": close - 0.04,
                        "close": close,
                        "volume": 200,
                        "price_proxy_kind": "candle_close",
                    })
            (self.root / f"data/w1/candles/{secid}_1h.json").write_text(json.dumps({
                "schema_version": "iss-candles.v1", "secid": secid,
                "interval": 60, "timeframe": "1h", "rows": hourly_rows,
            }))
        save_settings(self.root, {"timeframe": "1h", "validation_percent": 20})
        catalog = self.proof.catalog()
        self.assertEqual(catalog["timeframe"], "1h")
        pair = next(row for row in catalog["pair_screen"]["pairs"] if row["id"] == "CNYRUBF/CRZ6")
        self.assertTrue(pair["proof_strategies"]["mean_reversion"])
        self.assertFalse(pair["proof_strategies"]["funding_only"])
        self.assertFalse(catalog["assets"])

        selection = {
            "universe": "pair_screen", "pair": "CNYRUBF/CRZ6",
            "strategy": "mean_reversion", "sizing": "max_affordable",
            "parameters": {"lookback": 5, "entry_z": 0.8, "exit_z": 0.2},
        }
        result = self.proof.proof(selection)
        ids = [row["date"] for row in result["series"]]
        self.assertGreater(len(ids), 100)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(row["benchmark_mark_date"] < row["session_date"] for row in result["series"]))
        self.assertEqual(result["window"]["min_leg_volume"], 800)
        self.assertEqual(result["selection"]["timeframe"], "1h")
        from moex_spread_lab.proof_search import research_run
        evaluated = research_run(self.proof, selection, optimize=False, include_plateau=False)
        self.assertIsNotNone(evaluated["evaluation"]["periods"]["validation"])
        self.assertGreater(evaluated["evaluation"]["observations"]["validation"], 0)
        self.assertAlmostEqual(result["summary"]["ledger_reconciliation_rub"], 0.0, places=2)
        self.assertTrue(any("SWAPRATE не включён" in note for note in result["notes"]))

        cutoff = daily["rows"][10]["begin"][:10]
        replay = self.proof.prepare(
            selection,
            settings=ResearchSettings(timeframe="1h", validation_percent=20),
        )
        partial = replay(selection, through=cutoff)
        partial_sessions = [row["session_date"] for row in partial["series"]]
        self.assertTrue(partial_sessions)
        self.assertEqual(partial_sessions[-1], cutoff)
        self.assertTrue(all(day <= cutoff for day in partial_sessions))

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
        funding_rows = [row for row in result["series"] if row["funding_rub"] is not None]
        self.assertTrue(funding_rows)
        self.assertTrue(any(row["funding_rub"] != 0 for row in funding_rows))
        self.assertTrue(all(row["funding_swaprate_rub_per_unit"] is not None for row in funding_rows))
        self.assertTrue(any(trade["funding_rub"] is not None and trade["funding_rub"] != 0 for trade in result["trades"]))
        for trade in result["trades"]:
            if trade["funding_rub"]:
                expected_sign = -1 if trade["direction_detail"] == "long_near_short_far" else 1
                self.assertEqual(1 if trade["funding_rub"] > 0 else -1, expected_sign)
        self.assertAlmostEqual(sum(row["funding_rub"] or 0 for row in result["ledger"]),
                               sum(trade["funding_rub"] or 0 for trade in result["trades"]), delta=0.02)
        components = sum(sum((trade["entry"]["costs"] or {}).values()) + sum((trade["exit"]["costs"] or {}).values())
                         for trade in result["trades"])
        self.assertAlmostEqual(components, summary["costs_rub"], delta=0.02)
        first = result["trades"][0]
        self.assertEqual(first["entry"]["near_volume"], 5000)
        self.assertGreater(first["entry"]["costs"]["exchange_clearing"], 0)
        self.assertEqual(first["signal"]["kind"], "z")

    def test_zero_volume_leg_is_journal_only_and_cannot_be_an_execution_date(self):
        far_path = self.root / "data/w1/candles/CRZ6_1d.json"
        payload = json.loads(far_path.read_text())
        zero_day = payload["rows"][30]["begin"][:10]
        payload["rows"][30]["volume"] = 0
        far_path.write_text(json.dumps(payload))
        result = self.proof.proof({"universe": "pair_screen", "pair": "CNYRUBF/CRZ6",
                                   "strategy": "mean_reversion", "quantity": 1, "slippage_ticks": 1.0,
                                   "parameters": {"entry_z": 1.5, "exit_z": 0.3, "lookback": 20}})
        journal_row = next(row for row in result["series"] if row["date"] == zero_day)
        journal_ledger = next(row for row in result["ledger"] if row["date"] == zero_day)
        self.assertEqual(journal_row["far_volume"], 0)
        self.assertFalse(journal_row["volume_eligible"])
        self.assertIsNone(journal_row["signal_z"])
        self.assertEqual(journal_ledger["mark_pnl_rub"], 0)
        self.assertTrue(all(trade[side]["date"] != zero_day for trade in result["trades"] for side in ("entry", "exit")))

    def test_series_and_fills_spread_is_relative_to_far_contract_value(self):
        result = self.proof.proof({"universe": "pair_screen", "pair": "CNYRUBF/CRZ6",
                                   "strategy": "mean_reversion", "quantity": 2, "slippage_ticks": 1.0,
                                   "parameters": {"entry_z": 1.5, "exit_z": 0.3, "lookback": 20}})
        far_mult = 1 / 0.001  # tick_value / tick_size from the fixture's current-intake.json
        for row in result["series"]:
            self.assertAlmostEqual(row["spread_rel"], row["spread"] / (row["far"] * far_mult) * 100, delta=1e-4)
            self.assertAlmostEqual(row["spread_open_rel"], (row["near_open"] * far_mult - row["far_open"] * far_mult) / (row["far_open"] * far_mult) * 100, delta=1e-4)
        for trade in result["trades"]:
            for side in ("entry", "exit"):
                block = trade[side]
                self.assertAlmostEqual(block["spread_fill_rel"], block["spread_fill"] / (block["far_price"] * far_mult) * 100, delta=1e-4)

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
        self.assertTrue(all(row["funding_swaprate_rub_per_unit"] is not None for row in funding_rows))
        self.assertTrue(all(row["funding_lot_size"] > 0 and row["funding_contracts"] > 0 for row in funding_rows))
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

    def test_routes_tolerate_query_strings(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(self.root))))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/proof?universe=pair_screen&pair=CNYRUBF/CRZ6&strategy=mean_reversion")
        page = connection.getresponse()
        self.assertEqual(page.status, 200)
        self.assertIn("Доказательство исполнения".encode(), page.read())
        connection.request("GET", "/api/proof/catalog?universe=pair_screen")
        catalog = connection.getresponse()
        self.assertEqual(catalog.status, 200)
        self.assertIn("pair_screen", json.loads(catalog.read()))
        connection.request("GET", "/vendor/lightweight-charts.standalone.production.js?v=1")
        vendor = connection.getresponse()
        self.assertEqual(vendor.status, 200)
        vendor.read()
        connection.close()


if __name__ == "__main__":
    unittest.main()
