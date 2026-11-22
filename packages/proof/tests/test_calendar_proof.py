import http.client
import json
import math
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.calendar.calendar_spreads import (
    HISTORY_FIELDS,
    HISTORY_FROM,
    CalendarData,
)
from moex_spread_lab.strategies.funding_carry.carry import _history_url
from moex_spread_lab.web.server import ScreenerInputs, ScreenerStore, make_handler

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
        "history.cursor": {
            "columns": ["INDEX", "TOTAL", "PAGESIZE"],
            "data": [[0, len(rows), 100]],
        },
    }
    path = root / "data/spread-history-cache" / f"{secid}-{HISTORY_FROM}-{TILL}-0.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"url": url, "payload": payload, "raw_response_sha256": "x"}), encoding="utf-8"
    )


def _write_h1_candles(root: Path, secid: str, rows: list[dict]) -> None:
    path = root / "data/w1/candles" / f"{secid}_1h.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "iss-candles.v1",
                "secid": secid,
                "interval": 60,
                "rows": rows,
            }
        ),
        encoding="utf-8",
    )


def _h1_rows(role: str, volume: int, first_session_volume: int | None = None) -> list[dict]:
    rows = []
    for day_index, day in enumerate(("2022-12-13", "2022-12-14", "2022-12-15")):
        for bar_index, hour in enumerate(range(9, 21)):
            i = day_index * 8 + bar_index
            near = 30000.0 + 2.0 * math.sin(i / 2.0)
            far = 31000.0 + 32.0 * math.sin(i / 2.0) + (65.0 if i % 11 == 8 else 0.0)
            native = 1001.0 + 18.0 * math.sin(i / 2.0) + (40.0 if i % 11 == 8 else 0.0)
            close = {"near": near, "far": far, "spread": native}[role]
            rows.append(
                {
                    "begin": f"{day}T{hour:02}:00:00",
                    "open": close - 1.0,
                    "close": close,
                    "volume": first_session_volume
                    if day_index == 0 and first_session_volume is not None
                    else volume,
                }
            )
    return rows


def _build_root(root: Path) -> None:
    near_days = _weekdays(date(2022, 12, 12), date(2023, 3, 16))
    far_days = _weekdays(date(2022, 12, 12), date(2023, 6, 15))
    far_price = {
        d: 31000 + 40 * math.sin(i / 2) + (350 if i == 30 else 0) for i, d in enumerate(far_days)
    }
    row = lambda secid, d, settle, volume, opening: [
        d,
        secid,
        opening,
        settle + 5,
        settle - 5,
        settle,
        settle,
        volume,
        20,
    ]
    _write_history(root, "SRH3", [row("SRH3", d, 30000.0, 9000, 29990.0) for d in near_days])
    _write_history(
        root, "SRM3", [row("SRM3", d, far_price[d], 3000, far_price[d] + 8) for d in far_days]
    )
    _write_history(
        root, "SRH3SRM3", [row("SRH3SRM3", d, far_price[d] - 30000.0, 400, 0.0) for d in near_days]
    )
    (root / "data/w1").mkdir(parents=True, exist_ok=True)
    (root / "data/w1/current-intake.json").write_text(
        json.dumps(
            {
                "instruments": [
                    {
                        "secid": "SRZ6",
                        "expiry": "2026-12-17",
                        "tick_size": 1.0,
                        "tick_value": 1.0,
                        "initial_margin": 3000.0,
                        "settlement": 30000.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (root / "data/benchmark-cache").mkdir(parents=True, exist_ok=True)
    (root / "data/benchmark-cache/CBR_RUONIA.json").write_text(
        json.dumps({"rows": [{"date": "2022-01-01", "rate": 15.0}]}), encoding="utf-8"
    )
    (root / "data/benchmark").mkdir(parents=True, exist_ok=True)
    (root / "data/benchmark/lqdt-lineage.json").write_text(
        json.dumps(
            {"rows": [{"date": d, "price": 1.0 + 0.0004 * i} for i, d in enumerate(far_days)]}
        ),
        encoding="utf-8",
    )


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

    def test_series_and_fills_spread_is_relative_to_far_price(self):
        proof = self.data.proof(
            {
                "asset": "SR",
                "pair": "SRH3/SRM3",
                "strategy": "zscore_mean_reversion",
                "fill": "next_open",
                "quantity": 2,
                "slippage_ticks": 1.0,
                "parameters": {"entry_z": 1.5, "exit_z": 0.5, "window": 20},
            }
        )
        for row in proof["series"]:
            self.assertAlmostEqual(row["spread_rel"], row["spread"] / row["far"] * 100, delta=1e-4)
            if row["far_open"]:
                self.assertAlmostEqual(
                    row["spread_open_rel"], row["spread_open"] / row["far_open"] * 100, delta=1e-4
                )
        for trade in proof["trades"]:
            for side in ("entry", "exit"):
                fill = trade[side]
                far = fill["far_price"]
                self.assertAlmostEqual(
                    fill["spread_fill_rel"], fill["spread_fill"] / far * 100, delta=1e-4
                )

    def test_zero_volume_spread_date_stays_in_journal_but_is_not_an_analysis_or_fill_day(self):
        path = self.root / "data/spread-history-cache/SRH3SRM3-2022-06-01-2023-06-15-0.json"
        payload = json.loads(path.read_text())
        block = payload["payload"]["history"]
        volume_index = block["columns"].index("VOLUME")
        date_index = block["columns"].index("TRADEDATE")
        zero_day = block["data"][30][date_index]
        block["data"][30][volume_index] = 0
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.data._loaded = None
        proof = self.data.proof(
            {
                "asset": "SR",
                "pair": "SRH3/SRM3",
                "strategy": "zscore_mean_reversion",
                "fill": "next_open",
                "quantity": 1,
                "slippage_ticks": 1.0,
                "parameters": {"entry_z": 1.5, "exit_z": 0.5, "window": 20},
            }
        )
        row = next(item for item in proof["series"] if item["date"] == zero_day)
        ledger = next(item for item in proof["ledger"] if item["date"] == zero_day)
        self.assertEqual(row["spread_volume"], 0)
        self.assertFalse(row["volume_eligible"])
        self.assertEqual(ledger["mark_pnl_rub"], 0)
        self.assertTrue(
            all(
                trade[side]["date"] != zero_day
                for trade in proof["trades"]
                for side in ("entry", "exit")
            )
        )
        self.assertEqual(proof["window"]["min_spread_volume"], 400)

    def test_proof_reconciles_ledger_to_trades_and_is_strict_json(self):
        proof = self.data.proof(
            {
                "asset": "SR",
                "pair": "SRH3/SRM3",
                "strategy": "zscore_mean_reversion",
                "fill": "next_open",
                "quantity": 2,
                "slippage_ticks": 1.0,
                "parameters": {"entry_z": 1.5, "exit_z": 0.5, "window": 20},
            }
        )
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
        self.assertAlmostEqual(
            proof["ledger"][-1]["cumulative_net_rub"], proof["summary"]["net_rub"], places=1
        )

    def test_invalid_selections_are_rejected(self):
        base = {"asset": "SR", "pair": "SRH3/SRM3"}
        for bad in (
            {**base, "quantity": 0},
            {**base, "asset": "XX"},
            {**base, "pair": "SRM3/SRU3"},
            {**base, "strategy": "x"},
            {**base, "parameters": {"unknown": 1}},
            {**base, "parameters": {"entry_z": -1}},
        ):
            with self.assertRaises(W0Error):
                self.data.proof(bad)

    def test_http_routes_serve_page_catalog_and_proof(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(self.root)))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/proof")
        page = connection.getresponse()
        self.assertEqual(page.status, 200)
        self.assertIn("Доказательство исполнения".encode(), page.read())
        connection.request("GET", "/api/proof/catalog")
        catalog = connection.getresponse()
        self.assertEqual(catalog.status, 200)
        catalog.read()
        headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{server.server_port}",
        }
        connection.request(
            "POST", "/api/proof/run", json.dumps({"asset": "SR", "pair": "SRH3/SRM3"}), headers
        )
        run = connection.getresponse()
        self.assertEqual(run.status, 200)
        self.assertEqual(json.loads(run.read())["selection"]["pair"], "SRH3/SRM3")
        connection.request(
            "POST", "/api/proof/run", json.dumps({"asset": "SR", "pair": "nope"}), headers
        )
        bad = connection.getresponse()
        self.assertEqual(bad.status, 400)
        bad.read()
        connection.close()

    def test_h1_native_spread_has_priority_and_uses_its_own_session_volume_gate(self):
        _write_h1_candles(self.root, "SRH3", _h1_rows("near", 100))
        _write_h1_candles(self.root, "SRM3", _h1_rows("far", 100))
        _write_h1_candles(self.root, "SRH3SRM3", _h1_rows("spread", 5))

        window = self.data.window("SR", "SRH3/SRM3", "1h")
        self.assertEqual(window["price_source"], "native_h1_spread")
        self.assertTrue(window["has_native_spread_volume"])
        self.assertTrue(window["native_spread_order"])
        self.assertEqual(window["liquidity_source"], "native_spread_session_volume")
        self.assertEqual(window["liquidity_gate"], 100)
        self.assertEqual(window["min_liquidity_volume"], 60)
        self.assertFalse(window["tradable_every_session"])
        self.assertEqual(window["rows"][0]["spread_volume"], 5)
        self.assertNotEqual(window["rows"][0]["spread_price"], 1000.0)

    def test_h1_proxy_is_synchronized_causal_and_reconciles(self):
        _write_h1_candles(self.root, "SRH3", _h1_rows("near", 20))
        _write_h1_candles(self.root, "SRM3", _h1_rows("far", 20))

        proof = self.data.proof(
            {
                "asset": "SR",
                "pair": "SRH3/SRM3",
                "timeframe": "1h",
                "strategy": "zscore_mean_reversion",
                "fill": "next_open",
                "quantity": 1,
                "slippage_ticks": 1.0,
                "parameters": {"entry_z": 1.5, "exit_z": 0.5, "window": 3},
            }
        )
        window = proof["window"]
        self.assertEqual(window["price_source"], "synthetic_leg_proxy")
        self.assertEqual(window["native_fallback_reason"], "no_local_h1_file")
        self.assertFalse(window["has_native_spread_volume"])
        self.assertEqual(window["liquidity_source"], "matched_near_far_session_volume")
        self.assertEqual(window["liquidity_gate_unit"], "контр./сессию")
        self.assertEqual(window["min_liquidity_volume"], 240)
        self.assertTrue(window["tradable_every_session"])
        self.assertEqual(len(proof["series"]), 36)
        self.assertEqual(len({row["timestamp"] for row in proof["series"]}), 36)
        self.assertTrue(all(row["spread_volume"] is None for row in proof["series"]))
        self.assertEqual(proof["series"][0]["ruonia_rate_date"], "2022-01-01")
        self.assertEqual(proof["series"][0]["benchmark_mark_date"], "2022-12-12")
        self.assertTrue(any(not row["signal_eligible"] for row in proof["series"]))
        self.assertTrue(any(row["signal_eligible"] for row in proof["series"]))
        # Daily mark rounding can differ from the per-trade rounded sum by one kopeck.
        self.assertLessEqual(abs(proof["summary"]["ledger_reconciliation_rub"]), 0.01)
        json.dumps(proof, allow_nan=False)
        convergence = self.data.proof(
            {
                "asset": "SR",
                "pair": "SRH3/SRM3",
                "timeframe": "1h",
                "strategy": "convergence_to_ruonia",
                "fill": "next_open",
                "quantity": 1,
                "slippage_ticks": 1.0,
                "parameters": {"threshold_pp": 0.1},
            }
        )
        self.assertEqual(convergence["selection"]["strategy"], "convergence_to_ruonia")
        self.assertEqual(convergence["series"][0]["ruonia_rate_date"], "2022-01-01")
        self.assertTrue(all(row["dev_pp"] is not None for row in convergence["series"]))

    def test_h1_training_gate_uses_only_the_training_prefix(self):
        _write_h1_candles(self.root, "SRH3", _h1_rows("near", 20, first_session_volume=5))
        _write_h1_candles(self.root, "SRM3", _h1_rows("far", 20, first_session_volume=5))
        strict = ResearchSettings(
            validation_percent=25,
            allow_failed_volume=False,
            allow_failed_capital=True,
            initial_capital_rub=100000.0,
            timeframe="1h",
        )
        with self.assertRaisesRegex(W0Error, "ликвидности на обучении"):
            self.data.check_training_window("SR", "SRH3/SRM3", 1, strict)
        permissive = ResearchSettings(
            validation_percent=25,
            allow_failed_volume=True,
            allow_failed_capital=True,
            initial_capital_rub=100000.0,
            timeframe="1h",
        )
        self.data.check_training_window("SR", "SRH3/SRM3", 1, permissive)

    def test_http_h1_catalog_and_proof_route(self):
        _write_h1_candles(self.root, "SRH3", _h1_rows("near", 20))
        _write_h1_candles(self.root, "SRM3", _h1_rows("far", 20))
        historical_path = self.root / "data/w1/historical-contracts.json"
        historical_path.write_text(
            json.dumps(
                {
                    "instruments": [
                        {
                            "secid": secid,
                            "initial_margin": 3000.0,
                            "tick_size": 1.0,
                            "tick_value": 1.0,
                            "tariff_group": "stock",
                        }
                        for secid in ("SRH3", "SRM3")
                    ]
                }
            ),
            encoding="utf-8",
        )
        settings_path = self.root / "data/analytics/research-settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(
                {
                    "validation_percent": 0,
                    "allow_failed_volume": False,
                    "allow_failed_capital": False,
                    "slippage_ticks": 1.0,
                    "initial_capital_rub": 100000.0,
                    "timeframe": "1h",
                }
            ),
            encoding="utf-8",
        )

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(self.root)))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/api/proof/catalog")
        catalog_response = connection.getresponse()
        self.assertEqual(catalog_response.status, 200)
        catalog = json.loads(catalog_response.read())
        self.assertEqual(catalog["timeframe"], "1h")
        by_id = {item["id"]: item for item in catalog["timeframes"]}
        self.assertTrue(by_id["1h"]["supports_calendar"])
        self.assertEqual([item["id"] for item in catalog["timeframes"]], ["1h", "1d"])
        catalog_window = next(
            window
            for asset in catalog["assets"]
            if asset["asset"] == "SR"
            for window in asset["windows"]
            if window["pair"] == "SRH3/SRM3"
        )
        self.assertEqual(catalog_window["price_source"], "synthetic_leg_proxy")
        self.assertEqual(catalog_window["liquidity_source"], "matched_near_far_session_volume")

        headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{server.server_port}",
        }
        request = {
            "universe": "calendar_native",
            "asset": "SR",
            "pair": "SRH3/SRM3",
            "strategy": "zscore_mean_reversion",
            "fill": "next_open",
            "sizing": "max_affordable",
            "parameters": {"entry_z": 1.5, "exit_z": 0.5, "window": 3},
        }
        connection.request("POST", "/api/proof/run", json.dumps(request), headers)
        response = connection.getresponse()
        body = response.read()
        self.assertEqual(response.status, 200, body.decode())
        proof = json.loads(body)
        self.assertEqual(proof["selection"]["timeframe"], "1h")
        self.assertEqual(proof["window"]["price_source"], "synthetic_leg_proxy")
        self.assertEqual(proof["window"]["liquidity_gate_unit"], "контр./сессию")
        self.assertEqual(proof["summary"]["ledger_reconciliation_rub"], 0.0)
        self.assertEqual(
            proof["summary"]["closed_trades"],
            sum(not trade["open_at_end"] for trade in proof["trades"]),
        )
        self.assertEqual(proof["sizing"]["policy"], "max-affordable.v1")
        self.assertGreater(proof["summary"]["max_quantity"], 0)
        self.assertEqual(proof["evaluation"]["failed_screens"], [])
        self.assertGreater(proof["summary"]["cost_totals"]["exchange_clearing"], 0)
        self.assertGreater(proof["summary"]["cost_totals"]["slippage"], 0)
        json.dumps(proof, allow_nan=False)
        connection.close()

    def test_service_calendar_payload_adds_instrument_provenance_and_run_id(self):
        from moex_spread_lab.proof_runtime.service import ProofService

        proof = ProofService(self.root).proof(
            {
                "universe": "calendar_native",
                "asset": "SR",
                "pair": "SRH3/SRM3",
                "strategy": "zscore_mean_reversion",
                "fill": "next_open",
                "quantity": 2,
                "slippage_ticks": 1.0,
                "parameters": {"entry_z": 1.5, "exit_z": 0.5, "window": 20},
            }
        )
        self.assertEqual(proof["instrument"]["near"], "SRH3")
        self.assertEqual(proof["instrument"]["far"], "SRM3")
        self.assertEqual(proof["instrument"]["spread_instrument"], "SRH3SRM3")
        self.assertEqual(proof["instrument"]["near_expiry"], "2023-03-16")
        provenance = proof["provenance"]
        for key in (
            "near_history_sha256",
            "far_history_sha256",
            "spread_history_sha256",
            "contracts_sha256",
            "benchmark_sha256",
            "ruonia_sha256",
        ):
            self.assertTrue(provenance.get(key), key)
        self.assertEqual(len(proof["run_id"]), 16)
        self.assertIn("календарн", proof["price_role"].lower())


if __name__ == "__main__":
    unittest.main()
