"""Synthetic workspaces shared by the proof equivalence tiers (plan S4a).

Builders moved out of ``test_perf_equivalence.py`` so the canary and every
full-resolution tier clone identical bytes. This module only writes files: it
constructs no proof service and holds no numeric expectation.

The helper names (``_write_h1_candles``, ``_build_h1_workspace``, ``_canon``,
``_canon_sha``) are re-exported by the canary ``test_perf_equivalence``, because the
drop-in plugin integration test imports them from there. The class lifecycle and
the reference-vs-fast comparison live in ``perf_equivalence_case``.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path

from moex_spread_lab.strategies.calendar.calendar_spreads import HISTORY_FIELDS, HISTORY_FROM
from moex_spread_lab.strategies.funding_carry.carry import _history_url
from proof_fixtures import fixture
from synthetic_history import iso_weekdays as _weekdays
from synthetic_history import write_h1_candles as _write_h1_candles

TILL = "2023-06-15"


def _canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canon_sha(value) -> str:
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def _extend_benchmark(
    root: Path, h1_days: list[str], cal_days: list[str], sru3_days: list[str]
) -> None:
    """Add the LQDT marks the 1h pair and calendar windows need, in date order."""
    lqdt_path = root / "data/benchmark/lqdt-lineage.json"
    lqdt = json.loads(lqdt_path.read_text(encoding="utf-8"))
    existing_dates = {row["date"] for row in lqdt["rows"]}
    for series, price_base, price_step in (
        (cal_days, 0.85, 0.00035),
        (h1_days, 1.02, 0.00045),
        (sru3_days, 0.88, 0.00035),
    ):
        for idx, day in enumerate(series):
            if day in existing_dates:
                continue
            lqdt["rows"].append(
                {
                    "date": day,
                    "price": round(price_base + idx * price_step, 6),
                    "source_kind": "synthetic",
                    "source_ticker": "LQDT",
                }
            )
            existing_dates.add(day)
    # A prior benchmark date is required before the first 1h bar of each window.
    for anchor, price in (("2022-12-09", 0.8495), ("2026-01-02", 1.0195)):
        if anchor not in existing_dates:
            lqdt["rows"].append(
                {
                    "date": anchor,
                    "price": price,
                    "source_kind": "synthetic",
                    "source_ticker": "LQDT",
                }
            )
            existing_dates.add(anchor)
    lqdt["rows"].sort(key=lambda row: row["date"])
    lqdt_path.write_text(json.dumps(lqdt), encoding="utf-8")


def _add_pair_contracts(root: Path) -> None:
    """CRM6 and the historical SR contracts the 1h pair/calendar windows read."""
    intake_path = root / "data/w1/current-intake.json"
    intake = json.loads(intake_path.read_text(encoding="utf-8"))
    known_secids = {item["secid"] for item in intake["instruments"]}
    if "CRM6" not in known_secids:
        intake["instruments"].append(
            {
                "secid": "CRM6",
                "lot_size": 1000,
                "tick_size": 0.001,
                "tick_value": 1.0,
                "initial_margin": 1900.0,
                "settlement": 13.45,
                "expiry": "2026-06-18",
                "tariff_group": "fx_precious",
            }
        )
    if "SRZ6" not in known_secids:
        intake["instruments"].append(
            {
                "secid": "SRZ6",
                "lot_size": 100,
                "tick_size": 1.0,
                "tick_value": 1.0,
                "initial_margin": 3000.0,
                "settlement": 30000.0,
                "expiry": "2026-12-17",
                "tariff_group": "stock",
            }
        )
    intake_path.write_text(json.dumps(intake), encoding="utf-8")
    hist_path = root / "data/w1/historical-contracts.json"
    hist_path.write_text(
        json.dumps(
            {
                "instruments": [
                    {
                        "secid": secid,
                        "initial_margin": 2500.0,
                        "tick_size": 1.0,
                        "tick_value": 1.0,
                        "tariff_group": "stock",
                    }
                    for secid in ("SRH3", "SRM3", "SRU3")
                ]
            }
        ),
        encoding="utf-8",
    )


def _pair_bar_prices(bar_idx: int) -> tuple[float, float, float, int]:
    base = 13.50 + 0.015 * math.sin(bar_idx / 2.3)
    shock_z6 = 0.095 if bar_idx % 13 == 6 else (-0.085 if bar_idx % 17 == 11 else 0.0)
    shock_m6 = -0.090 if bar_idx % 11 == 5 else (0.080 if bar_idx % 15 == 9 else 0.0)
    perp_close = round(base, 4)
    z6_close = round(base + 0.22 + 0.028 * math.cos(bar_idx / 3.1) + shock_z6, 4)
    m6_close = round(base + 0.11 + 0.024 * math.sin(bar_idx / 2.7) + shock_m6, 4)
    vol_z6 = 0 if bar_idx == 19 else 60
    return perp_close, z6_close, m6_close, vol_z6


def _build_pair_h1_candles(root: Path, h1_days: list[str]) -> None:
    """CNYRUBF/CRZ6/CRM6 1h candles, including one zero-volume bar."""
    perp_rows, crz6_rows, crm6_rows = [], [], []
    bar_idx = 0
    for day in h1_days[:12]:
        for hour in range(10, 18):
            ts = f"{day}T{hour:02d}:00:00"
            perp_close, z6_close, m6_close, vol_z6 = _pair_bar_prices(bar_idx)
            perp_rows.append(
                {
                    "begin": ts,
                    "open": round(perp_close - 0.002, 4),
                    "close": perp_close,
                    "volume": 120,
                }
            )
            crz6_rows.append(
                {
                    "begin": ts,
                    "open": round(z6_close + 0.003, 4),
                    "close": z6_close,
                    "volume": vol_z6,
                }
            )
            crm6_rows.append(
                {"begin": ts, "open": round(m6_close - 0.002, 4), "close": m6_close, "volume": 55}
            )
            bar_idx += 1
    _write_h1_candles(root, "candles-screen", "CNYRUBF", perp_rows)
    _write_h1_candles(root, "candles-screen", "CRZ6", crz6_rows)
    _write_h1_candles(root, "candles-screen", "CRM6", crm6_rows)


def _write_cal_eod(root: Path, secid: str, days: list[str], row_fn) -> None:
    url = _history_url(secid, HISTORY_FROM, TILL, 0, HISTORY_FIELDS)
    rows = [row_fn(i, d) for i, d in enumerate(days)]
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
        json.dumps({"url": url, "payload": payload, "raw_response_sha256": f"sha-{secid}"}),
        encoding="utf-8",
    )


def _build_h1_workspace(root: Path) -> None:
    """Create a rich 1h + 1d synthetic workspace with pair_screen and calendar_native windows.

    The same bytes as the single builder this replaces in ``test_perf_equivalence.py``;
    the steps are named so no single function carries the whole fixture.
    """
    fixture(root)
    h1_days = _weekdays(date(2026, 1, 5), 18)
    cal_days = _weekdays(date(2022, 12, 12), 70)
    sru3_days = _weekdays(date(2023, 1, 9), 90)
    _extend_benchmark(root, h1_days, cal_days, sru3_days)
    _add_pair_contracts(root)
    _build_pair_h1_candles(root, h1_days)
    srh3_days = [d for d in cal_days if d <= "2023-03-16"]
    srm3_days = [d for d in cal_days if d <= "2023-06-15"]
    overlap_m3_u3 = [d for d in srm3_days if d in set(sru3_days)]
    _write_calendar_eod_pages(root, srh3_days, srm3_days, sru3_days, overlap_m3_u3)
    _build_calendar_h1(root, srh3_days, overlap_m3_u3)


def _mk_cal_row(secid: str, day: str, settle: float, vol: int, op: float) -> list:
    return [day, secid, op, settle + 5.0, settle - 5.0, settle, settle, vol, 25]


def _write_calendar_eod_pages(
    root: Path,
    srh3_days: list[str],
    srm3_days: list[str],
    sru3_days: list[str],
    overlap_m3_u3: list[str],
) -> None:
    """Calendar EOD history pages for SRH3/SRM3, SRU3 and SRM3SRU3."""
    _write_cal_eod(
        root,
        "SRH3",
        srh3_days,
        lambda i, d: _mk_cal_row("SRH3", d, 30000.0 + 10.0 * math.sin(i / 3.0), 8000, 29995.0),
    )
    _write_cal_eod(
        root,
        "SRM3",
        srm3_days,
        lambda i, d: _mk_cal_row(
            "SRM3",
            d,
            31050.0 + 45.0 * math.sin(i / 2.2) + (280.0 if i % 19 == 10 else 0.0),
            4000,
            31040.0,
        ),
    )
    _write_cal_eod(
        root,
        "SRH3SRM3",
        srh3_days,
        lambda i, d: _mk_cal_row(
            "SRH3SRM3",
            d,
            1050.0 + 35.0 * math.sin(i / 2.2) + (260.0 if i % 19 == 10 else 0.0),
            350,
            1045.0,
        ),
    )
    _write_cal_eod(
        root,
        "SRU3",
        sru3_days,
        lambda i, d: _mk_cal_row(
            "SRU3",
            d,
            32100.0 + 50.0 * math.cos(i / 2.5) + (240.0 if i % 17 == 8 else 0.0),
            3500,
            32090.0,
        ),
    )
    _write_cal_eod(
        root,
        "SRM3SRU3",
        overlap_m3_u3,
        lambda i, d: _mk_cal_row("SRM3SRU3", d, 1050.0 + 30.0 * math.cos(i / 2.5), 300, 1045.0),
    )


def _build_calendar_h1(root: Path, srh3_days: list[str], overlap_m3_u3: list[str]) -> None:
    """1h candles for the synthetic-leg-proxy and native-spread calendar windows."""
    # SRH3/SRM3 has no SRH3SRM3_1h.json (synthetic_leg_proxy); SRM3/SRU3 has a native spread.
    srh3_h1, srm3_h1, sru3_h1, native_m3u3_h1 = [], [], [], []
    k = 0
    for day in srh3_days[:8]:
        for hour in range(10, 19):
            ts = f"{day}T{hour:02d}:00:00"
            near_c = round(30000.0 + 4.0 * math.sin(k / 2.0), 2)
            far_c = round(31020.0 + 38.0 * math.sin(k / 2.1) + (75.0 if k % 11 == 6 else 0.0), 2)
            srh3_h1.append({"begin": ts, "open": near_c - 1.0, "close": near_c, "volume": 40})
            srm3_h1.append({"begin": ts, "open": far_c - 1.0, "close": far_c, "volume": 35})
            k += 1
    k = 0
    for day in overlap_m3_u3[:8]:
        for hour in range(10, 19):
            ts = f"{day}T{hour:02d}:00:00"
            m3_c = round(31050.0 + 5.0 * math.cos(k / 2.0), 2)
            u3_c = round(32080.0 + 34.0 * math.cos(k / 2.2) + (68.0 if k % 10 == 4 else 0.0), 2)
            sp_c = round((u3_c - m3_c) + 3.0 * math.sin(k / 1.7), 2)
            if not any(r["begin"] == ts for r in srm3_h1):
                srm3_h1.append({"begin": ts, "open": m3_c - 1.0, "close": m3_c, "volume": 35})
            sru3_h1.append({"begin": ts, "open": u3_c - 1.0, "close": u3_c, "volume": 30})
            native_m3u3_h1.append({"begin": ts, "open": sp_c - 1.0, "close": sp_c, "volume": 25})
            k += 1

    _write_h1_candles(root, "candles", "SRH3", srh3_h1)
    _write_h1_candles(root, "candles", "SRM3", srm3_h1)
    _write_h1_candles(root, "candles", "SRU3", sru3_h1)
    _write_h1_candles(root, "candles", "SRM3SRU3", native_m3u3_h1)
