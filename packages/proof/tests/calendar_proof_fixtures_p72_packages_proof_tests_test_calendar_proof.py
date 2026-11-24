"""Shared synthetic calendar fixtures for the proof calendar tests.

Extracted under P7.2 test-health work (Issue #121) so
``test_calendar_proof`` meets the code-health limits without its size
baseline entry. Holds the ``TILL`` constant, the weekday/history/candle
writers, the synthetic root builder and the HTTP proof-UI scaffolding
shared by the two HTTP tests.
"""

import json
import math
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from moex_spread_lab.strategies.calendar.calendar_spreads import (
    HISTORY_FIELDS,
    HISTORY_FROM,
)
from moex_spread_lab.strategies.funding_carry.carry import _history_url
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs
from moex_spread_lab.web.server import ScreenerStore, make_handler

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


def _zscore_request(quantity: int = 2, window: int = 20, timeframe: str | None = None) -> dict:
    """The common ``zscore_mean_reversion`` proof request used by the calendar tests."""
    request = {
        "asset": "SR",
        "pair": "SRH3/SRM3",
        "strategy": "zscore_mean_reversion",
        "fill": "next_open",
        "quantity": quantity,
        "slippage_ticks": 1.0,
        "parameters": {"entry_z": 1.5, "exit_z": 0.5, "window": window},
    }
    if timeframe is not None:
        request["timeframe"] = timeframe
    return request


def _serve_proof_ui(testcase: unittest.TestCase, root: Path) -> ThreadingHTTPServer:
    """Serve the proof UI on an ephemeral port; cleanups mirror the inline blocks."""
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(root)))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    testcase.addCleanup(server.server_close)
    testcase.addCleanup(server.shutdown)
    return server


def _proof_headers(server: ThreadingHTTPServer) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Origin": f"http://127.0.0.1:{server.server_port}",
    }


def _write_h1_http_prereqs(root: Path) -> None:
    """Historical contracts and research settings for the H1 HTTP catalog/proof route."""
    historical_path = root / "data/w1/historical-contracts.json"
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
    settings_path = root / "data/analytics/research-settings.json"
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
