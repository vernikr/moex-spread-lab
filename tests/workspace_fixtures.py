"""Synthetic pair and calendar workspaces shared by proof and web tests."""

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path

from moex_spread_lab.data import HISTORY_FIELDS as PAIR_HISTORY_FIELDS
from moex_spread_lab.data import history_url
from moex_spread_lab.strategies.calendar.calendar_spreads import HISTORY_FIELDS, HISTORY_FROM
from synthetic_history import iso_weekdays_until as _weekdays
from synthetic_history import weekdays

TILL = "2023-06-15"
START, END = "2025-06-17", "2026-10-27"


def near_close(i: int) -> float:
    return 12 + 0.9 * math.sin(i * 0.55) + i * 0.012


def far_close(i: int) -> float:
    return 13.1 + 0.9 * math.sin(i * 0.55 + 0.4) + i * 0.012


def _write_carry_history(root: Path, secid: str, days: list[date], close) -> None:
    data = []
    for i, d in enumerate(days):
        settle = close(i)
        data.append(
            [
                "RFUD",
                d.isoformat(),
                secid,
                round(settle, 4),
                round(settle, 4),
                0.015,
                100000,
                500000.0,
                5000,
                round(settle, 4),
                250,
                "CNY",
            ]
        )
    path = root / "data" / "carry-history-cache" / f"{secid}-{START}-{END}-0.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    url = history_url(secid, START, END, 0, PAIR_HISTORY_FIELDS)
    payload = {
        "history": {"columns": list(PAIR_HISTORY_FIELDS), "data": data},
        "history.cursor": {
            "columns": ["INDEX", "TOTAL", "PAGESIZE"],
            "data": [[0, len(data), 100]],
        },
    }
    path.write_text(
        json.dumps(
            {
                "url": url,
                "payload": payload,
                "raw_response_sha256": "ab" * 32,
                "cached_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )


def build_pair_root(root: Path) -> list[date]:
    days = weekdays(date(2025, 6, 17), count=55)
    (root / "data/w1/candles").mkdir(parents=True)
    (root / "data/benchmark").mkdir(parents=True)
    (root / "data/w1").mkdir(parents=True, exist_ok=True)
    (root / "data/w1/current-intake.json").write_text(
        json.dumps(
            {
                "schema_version": "iss-intake.v1",
                "instruments": [
                    {
                        "secid": secid,
                        "stable_id": secid,
                        "board": "RFUD",
                        "expiry": "2026-12-17",
                        "lot_size": 1000,
                        "tick_size": 0.001,
                        "tick_value": 1,
                        "initial_margin": 1000,
                        "source_vintage": "current_snapshot",
                    }
                    for secid in ("CNYRUBF", "CRZ6")
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "data/benchmark/lqdt-lineage.json").write_text(
        json.dumps(
            {
                "schema_version": "lqdt-lineage.v1",
                "rows": [
                    {
                        "date": day.isoformat(),
                        "price": 1 + i * 0.0004,
                        "source_kind": "observed",
                        "source_ticker": "LQDT",
                    }
                    for i, day in enumerate(days)
                ],
            }
        ),
        encoding="utf-8",
    )
    for secid, close in (("CNYRUBF", near_close), ("CRZ6", far_close)):
        rows = []
        for i, day in enumerate(days):
            value = close(i)
            rows.append(
                {
                    "begin": f"{day.isoformat()} 00:00:00",
                    "open": round(value - 0.06, 4),
                    "close": round(value, 4),
                    "high": round(value + 0.1, 4),
                    "low": round(value - 0.1, 4),
                    "volume": 5000,
                    "price_proxy_kind": "candle_close",
                }
            )
        (root / f"data/w1/candles/{secid}_1d.json").write_text(
            json.dumps(
                {"schema_version": "iss-candles.v1", "secid": secid, "interval": 24, "rows": rows}
            ),
            encoding="utf-8",
        )
    _write_carry_history(root, "CNYRUBF", days, near_close)
    _write_carry_history(root, "CRZ6", days, far_close)
    return days


def write_calendar_history(root, secid, rows, *, response_hash):
    url = history_url(secid, HISTORY_FROM, TILL, 0, HISTORY_FIELDS)
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
        json.dumps(
            {
                "url": url,
                "payload": payload,
                "raw_response_sha256": response_hash,
                "cached_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )


def write_calendar_fixture_history(root: Path, secid: str, rows: list[list]) -> None:
    write_calendar_history(root, secid, rows, response_hash="x")


def build_calendar_root(root: Path) -> None:
    near_days = _weekdays(date(2022, 12, 12), date(2023, 3, 16))
    far_days = _weekdays(date(2022, 12, 12), date(2023, 6, 15))
    far_price = {
        d: 31000 + 40 * math.sin(i / 2) + (350 if i == 30 else 0) for i, d in enumerate(far_days)
    }

    def row(secid, d, settle, volume, opening):
        return [
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

    write_calendar_fixture_history(
        root, "SRH3", [row("SRH3", d, 30000.0, 9000, 29990.0) for d in near_days]
    )
    write_calendar_fixture_history(
        root, "SRM3", [row("SRM3", d, far_price[d], 3000, far_price[d] + 8) for d in far_days]
    )
    write_calendar_fixture_history(
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
