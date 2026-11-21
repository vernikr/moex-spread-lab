"""Bit-for-bit equivalence and cache-invalidation harness for proof search optimization."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from proof_fixtures import fixture, funding_winner

from moex_spread_lab.calendar_spreads import HISTORY_FIELDS, HISTORY_FROM
from moex_spread_lab.carry import _history_url, fetch_history
from moex_spread_lab.evaluation_periods import ResearchSettings, save_settings
from moex_spread_lab.position_sizing import AUTO_SIZING
from moex_spread_lab.proof import CALENDAR_UNIVERSE, PAIR_UNIVERSE, ProofService, reference_eval
from moex_spread_lab.proof_batch import checked_plateau
from moex_spread_lab.proof_search import research_run
from moex_spread_lab.proof_windows import window_summaries

TILL = "2023-06-15"


def _canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canon_sha(value) -> str:
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def _weekdays(start: date, count: int) -> list[str]:
    days, cur = [], start
    while len(days) < count:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def _write_h1_candles(root: Path, subdir: str, secid: str, rows: list[dict]) -> Path:
    path = root / "data/w1" / subdir / f"{secid}_1h.json"
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
    return path


def _build_h1_workspace(root: Path) -> None:
    """Create a rich 1h + 1d synthetic workspace with pair_screen and calendar_native windows."""
    # Start from standard 1d fixture so contracts, EOD carry, and 1d windows exist.
    fixture(root)

    # Extend benchmark + RUONIA + contracts for 1h pairs (CNYRUBF/CRZ6, CNYRUBF/CRM6) and calendar (SRH3/SRM3, SRM3/SRU3).
    h1_days = _weekdays(date(2026, 1, 5), 18)
    cal_days = _weekdays(date(2022, 12, 12), 70)

    lqdt_path = root / "data/benchmark/lqdt-lineage.json"
    lqdt = json.loads(lqdt_path.read_text(encoding="utf-8"))
    existing_dates = {row["date"] for row in lqdt["rows"]}
    for idx, day in enumerate(cal_days):
        if day not in existing_dates:
            lqdt["rows"].append(
                {
                    "date": day,
                    "price": round(0.85 + idx * 0.00035, 6),
                    "source_kind": "synthetic",
                    "source_ticker": "LQDT",
                }
            )
            existing_dates.add(day)
    for idx, day in enumerate(h1_days):
        if day not in existing_dates:
            lqdt["rows"].append(
                {
                    "date": day,
                    "price": round(1.02 + idx * 0.00045, 6),
                    "source_kind": "synthetic",
                    "source_ticker": "LQDT",
                }
            )
            existing_dates.add(day)
    # Include a prior benchmark date before the first 1h bar on 2022-12-12 and 2026-01-05.
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
    lqdt["rows"].sort(key=lambda r: r["date"])
    lqdt_path.write_text(json.dumps(lqdt), encoding="utf-8")

    # Add CRM6 + historical SR contracts.
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

    # Pair 1h candles: CNYRUBF shared across CRZ6 and CRM6, including a zero-volume bar to exercise journal vs analysis split.
    perp_rows, crz6_rows, crm6_rows = [], [], []
    bar_idx = 0
    for day in h1_days[:12]:
        for hour in range(10, 18):
            ts = f"{day}T{hour:02d}:00:00"
            base = 13.50 + 0.015 * math.sin(bar_idx / 2.3)
            shock_z6 = 0.095 if bar_idx % 13 == 6 else (-0.085 if bar_idx % 17 == 11 else 0.0)
            shock_m6 = -0.090 if bar_idx % 11 == 5 else (0.080 if bar_idx % 15 == 9 else 0.0)
            perp_close = round(base, 4)
            z6_close = round(base + 0.22 + 0.028 * math.cos(bar_idx / 3.1) + shock_z6, 4)
            m6_close = round(base + 0.11 + 0.024 * math.sin(bar_idx / 2.7) + shock_m6, 4)
            vol_z6 = 0 if bar_idx == 19 else 60
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
                {
                    "begin": ts,
                    "open": round(m6_close - 0.002, 4),
                    "close": m6_close,
                    "volume": 55,
                }
            )
            bar_idx += 1

    _write_h1_candles(root, "candles-screen", "CNYRUBF", perp_rows)
    _write_h1_candles(root, "candles-screen", "CRZ6", crz6_rows)
    _write_h1_candles(root, "candles-screen", "CRM6", crm6_rows)

    # Calendar EOD + 1h candles: SRH3/SRM3 (synthetic_leg_proxy) and SRM3/SRU3 (native_h1_spread).
    def write_cal_eod(secid: str, days: list[str], fn):
        url = _history_url(secid, HISTORY_FROM, TILL, 0, HISTORY_FIELDS)
        rows = [fn(i, d) for i, d in enumerate(days)]
        payload = {
            "history": {"columns": list(HISTORY_FIELDS), "data": rows},
            "history.cursor": {
                "columns": ["INDEX", "TOTAL", "PAGESIZE"],
                "data": [[0, len(rows), 100]],
            },
        }
        p = root / "data/spread-history-cache" / f"{secid}-{HISTORY_FROM}-{TILL}-0.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"url": url, "payload": payload, "raw_response_sha256": f"sha-{secid}"}),
            encoding="utf-8",
        )

    srh3_days = [d for d in cal_days if d <= "2023-03-16"]
    srm3_days = [d for d in cal_days if d <= "2023-06-15"]
    sru3_days = _weekdays(date(2023, 1, 9), 90)
    for idx, day in enumerate(sru3_days):
        if day not in existing_dates:
            lqdt["rows"].append(
                {
                    "date": day,
                    "price": round(0.88 + idx * 0.00035, 6),
                    "source_kind": "synthetic",
                    "source_ticker": "LQDT",
                }
            )
            existing_dates.add(day)
    lqdt["rows"].sort(key=lambda r: r["date"])
    lqdt_path.write_text(json.dumps(lqdt), encoding="utf-8")

    mk_row = lambda secid, d, settle, vol, op: [
        d, secid, op, settle + 5.0, settle - 5.0, settle, settle, vol, 25
    ]
    write_cal_eod("SRH3", srh3_days, lambda i, d: mk_row("SRH3", d, 30000.0 + 10.0 * math.sin(i / 3.0), 8000, 29995.0))
    write_cal_eod(
        "SRM3",
        srm3_days,
        lambda i, d: mk_row(
            "SRM3",
            d,
            31050.0 + 45.0 * math.sin(i / 2.2) + (280.0 if i % 19 == 10 else 0.0),
            4000,
            31040.0,
        ),
    )
    write_cal_eod(
        "SRH3SRM3",
        srh3_days,
        lambda i, d: mk_row(
            "SRH3SRM3",
            d,
            1050.0 + 35.0 * math.sin(i / 2.2) + (260.0 if i % 19 == 10 else 0.0),
            350,
            1045.0,
        ),
    )
    overlap_m3_u3 = [d for d in srm3_days if d in set(sru3_days)]
    write_cal_eod(
        "SRU3",
        sru3_days,
        lambda i, d: mk_row(
            "SRU3",
            d,
            32100.0 + 50.0 * math.cos(i / 2.5) + (240.0 if i % 17 == 8 else 0.0),
            3500,
            32090.0,
        ),
    )
    write_cal_eod(
        "SRM3SRU3",
        overlap_m3_u3,
        lambda i, d: mk_row("SRM3SRU3", d, 1050.0 + 30.0 * math.cos(i / 2.5), 300, 1045.0),
    )

    # 1h candles for SRH3 + SRM3 (synthetic_leg_proxy: no SRH3SRM3_1h.json)
    # and SRM3 + SRU3 + SRM3SRU3 (native_h1_spread).
    srh3_h1, srm3_h1, sru3_h1, native_m3u3_h1 = [], [], [], []
    k = 0
    for day in srh3_days[:8]:
        for hour in range(10, 19):
            ts = f"{day}T{hour:02d}:00:00"
            near_c = round(30000.0 + 4.0 * math.sin(k / 2.0), 2)
            far_c = round(
                31020.0 + 38.0 * math.sin(k / 2.1) + (75.0 if k % 11 == 6 else 0.0), 2
            )
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


class ProofPerfEquivalenceTests(unittest.TestCase):
    """Verify 100% bit-for-bit equality between reference evaluation and optimized fast path."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        _build_h1_workspace(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _compare_modes(self, settings: ResearchSettings, fn):
        with patch.object(ResearchSettings, "load", return_value=settings):
            with patch.dict(os.environ, {"MOEX_PROOF_REFERENCE_EVAL": "1"}):
                ref_service = ProofService(self.root)
                expected = fn(ref_service)
            with patch.dict(os.environ, {"MOEX_PROOF_REFERENCE_EVAL": "0"}):
                fast_service = ProofService(self.root)
                actual = fn(fast_service)
        self.assertEqual(_canon_sha(actual), _canon_sha(expected))
        self.assertEqual(actual, expected)
        return actual

    def test_1h_pair_screen_cohort_search_plateau_windows_and_single_run(self):
        settings = ResearchSettings(
            timeframe="1h",
            validation_percent=25,
            allow_failed_volume=True,
            allow_failed_capital=False,
            initial_capital_rub=30000.0,
            # Fixed seed: this test compares two runs, which must stay comparable.
            random_seed=41,
        )
        targets = [
            {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"},
            {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRM6"},
        ]

        def suite(service: ProofService):
            cat = service.catalog()
            opt = research_run(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                    "optimize_strategy": True,
                    "targets": targets,
                },
                optimize=True,
            )
            batch_only = research_run(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "fill": "reference_same_bar",
                    "sizing": AUTO_SIZING,
                    "optimize_strategy": False,
                    "targets": targets,
                },
                optimize=True,
            )
            plat = checked_plateau(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "parameters": {"lookback": 12, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                    "targets": targets,
                },
            )
            wins = window_summaries(
                service,
                {
                    "requests": [
                        {
                            "universe": PAIR_UNIVERSE,
                            "pair": t["pair"],
                            "strategy": "mean_reversion",
                            "parameters": {"lookback": 12, "entry_z": 1.2, "exit_z": 0.3},
                            "fill": "next_close",
                            "sizing": AUTO_SIZING,
                        }
                        for t in targets
                    ]
                },
            )
            single = research_run(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "parameters": {"lookback": 12, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                },
                optimize=False,
                include_plateau=True,
            )
            return {
                "catalog": cat,
                "opt": opt,
                "batch_only": batch_only,
                "plateau": plat,
                "windows": wins,
                "single": single,
            }

        res = self._compare_modes(settings, suite)
        self.assertEqual(res["opt"]["schema_version"], "proof-search.v4")
        self.assertEqual(res["opt"]["strategy_search"]["trials_per_strategy"], 80)

    def test_1h_calendar_proxy_and_native_cohort_and_mixed_universe(self):
        settings = ResearchSettings(
            timeframe="1h",
            validation_percent=20,
            allow_failed_volume=True,
            allow_failed_capital=True,
            initial_capital_rub=100000.0,
            # Fixed seed: these tests compare two runs, which must stay comparable.
            random_seed=41,
        )
        cal_targets = [
            {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"},
            {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRM3/SRU3"},
        ]
        mixed_targets = [
            {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"},
            {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"},
        ]

        def suite(service: ProofService):
            cal_opt = research_run(
                service,
                {
                    "universe": CALENDAR_UNIVERSE,
                    "asset": "SR",
                    "pair": "SRH3/SRM3",
                    "strategy": "zscore_mean_reversion",
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                    "optimize_strategy": True,
                    "targets": cal_targets,
                },
                optimize=True,
            )
            mixed_opt = research_run(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                    "optimize_strategy": True,
                    "targets": mixed_targets,
                },
                optimize=True,
            )
            plat = checked_plateau(
                service,
                {
                    "universe": CALENDAR_UNIVERSE,
                    "asset": "SR",
                    "pair": "SRH3/SRM3",
                    "strategy": "zscore_mean_reversion",
                    "parameters": {"window": 10, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                    "targets": cal_targets,
                },
            )
            wins = window_summaries(
                service,
                {
                    "requests": [
                        {
                            "universe": CALENDAR_UNIVERSE,
                            "asset": "SR",
                            "pair": t["pair"],
                            "strategy": "convergence_to_ruonia",
                            "parameters": {"threshold_pp": 0.5},
                            "fill": "next_open",
                            "sizing": AUTO_SIZING,
                        }
                        for t in cal_targets
                    ]
                },
            )
            return {
                "cal_opt": cal_opt,
                "mixed_opt": mixed_opt,
                "plateau": plat,
                "windows": wins,
            }

        res = self._compare_modes(settings, suite)
        self.assertEqual(res["cal_opt"]["schema_version"], "proof-search.v4")
        self.assertEqual(len(res["cal_opt"]["strategy_search"]["alternatives"]), 2)
        self.assertEqual(res["mixed_opt"]["schema_version"], "proof-search.v4")

    def test_1d_pair_funding_and_calendar_and_fixed_quantity_equivalence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            funding_winner(root)
            settings = ResearchSettings(
                timeframe="1d",
                validation_percent=20,
                allow_failed_volume=True,
                allow_failed_capital=True,
                initial_capital_rub=30000.0,
                # Fixed seed: these tests compare two runs, which must stay comparable.
                random_seed=41,
            )

            def run_for(env_flag: str):
                with (
                    patch.object(ResearchSettings, "load", return_value=settings),
                    patch.dict(os.environ, {"MOEX_PROOF_REFERENCE_EVAL": env_flag}),
                ):
                    svc = ProofService(root)
                    auto_pair = research_run(
                        svc,
                        {
                            "universe": PAIR_UNIVERSE,
                            "pair": "CNYRUBF/CRZ6",
                            "strategy": "mean_reversion",
                            "fill": "next_open",
                            "sizing": AUTO_SIZING,
                            "optimize_strategy": True,
                            "targets": [{"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"}],
                        },
                        optimize=True,
                    )
                    fixed_pair = research_run(
                        svc,
                        {
                            "universe": PAIR_UNIVERSE,
                            "pair": "CNYRUBF/CRZ6",
                            "strategy": "mean_reversion",
                            "fill": "next_open",
                            "quantity": 1,
                            "slippage_ticks": 1.0,
                            "optimize_strategy": False,
                            "targets": [{"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"}],
                        },
                        optimize=True,
                    )
                    cal_1d = research_run(
                        svc,
                        {
                            "universe": CALENDAR_UNIVERSE,
                            "asset": "SR",
                            "pair": "SRH3/SRM3",
                            "strategy": "zscore_mean_reversion",
                            "fill": "next_open",
                            "sizing": AUTO_SIZING,
                            "optimize_strategy": True,
                            "targets": [
                                {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"}
                            ],
                        },
                        optimize=True,
                    )
                    return {"auto_pair": auto_pair, "fixed_pair": fixed_pair, "cal_1d": cal_1d}

            expected = run_for("1")
            actual = run_for("0")
            self.assertEqual(_canon_sha(actual), _canon_sha(expected))
            self.assertEqual(actual, expected)

    def test_file_caches_invalidate_immediately_on_in_place_same_size_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_h1_workspace(root)
            save_settings(
                root,
                {
                    "timeframe": "1h",
                    "validation_percent": 0,
                    "allow_failed_volume": True,
                    "allow_failed_capital": True,
                    "initial_capital_rub": 100000.0,
                },
            )
            service = ProofService(root)

            # 1. Warm pair + calendar caches via catalog and proof runs.
            cat_before = service.catalog()
            pair_before = research_run(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                },
                optimize=False,
            )
            cal_before = research_run(
                service,
                {
                    "universe": CALENDAR_UNIVERSE,
                    "asset": "SR",
                    "pair": "SRH3/SRM3",
                    "strategy": "zscore_mean_reversion",
                    "parameters": {"window": 10, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                },
                optimize=False,
            )

            # 2. Mutate CRZ6_1h.json with the EXACT same byte length. A fresh
            #    (path, st_mtime_ns, st_size) key must invalidate the cache.
            crz6_path = root / "data/w1/candles-screen/CRZ6_1h.json"
            raw_crz6 = crz6_path.read_text(encoding="utf-8")
            mutated_crz6 = raw_crz6.replace('"volume": 60', '"volume": 61', 1)
            self.assertEqual(len(raw_crz6), len(mutated_crz6))
            before_stat = crz6_path.stat()
            time.sleep(0.02)  # guarantee a different st_mtime_ns on rewrite
            crz6_path.write_text(mutated_crz6, encoding="utf-8")
            after_stat = crz6_path.stat()
            self.assertNotEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)

            pair_after = research_run(
                service,
                {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                },
                optimize=False,
            )
            self.assertNotEqual(
                pair_before["provenance"]["far_candles_sha256"],
                pair_after["provenance"]["far_candles_sha256"],
            )
            self.assertNotEqual(pair_before["run_id"], pair_after["run_id"])

            # 3. Mutate the SRH3 EOD history cache file with the EXACT same byte
            #    length and verify the shared CalendarData picks up the fresh
            #    mtime/size key (calendar proofs read EOD history, not 1h candles,
            #    for near_history_sha256).
            eod_candidates = sorted(
                root.glob("data/spread-history-cache/SRH3-*-0.json")
            )
            self.assertTrue(eod_candidates, "SRH3 EOD history cache present")
            srh3_eod = eod_candidates[0]
            raw_srh3 = srh3_eod.read_text(encoding="utf-8")
            # Row 0 settles at 30000.0; replace the first occurrence with a
            # same-length value so st_size is unchanged but raw bytes (and sha256)
            # differ.
            self.assertIn("30000.0", raw_srh3)
            mutated_srh3 = raw_srh3.replace("30000.0", "30001.0", 1)
            self.assertEqual(len(raw_srh3), len(mutated_srh3))
            before_stat = srh3_eod.stat()
            time.sleep(0.02)  # guarantee a different st_mtime_ns on rewrite
            srh3_eod.write_text(mutated_srh3, encoding="utf-8")
            after_stat = srh3_eod.stat()
            self.assertNotEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)

            cal_after = research_run(
                service,
                {
                    "universe": CALENDAR_UNIVERSE,
                    "asset": "SR",
                    "pair": "SRH3/SRM3",
                    "strategy": "zscore_mean_reversion",
                    "parameters": {"window": 10, "entry_z": 1.2, "exit_z": 0.3},
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                },
                optimize=False,
            )
            self.assertNotEqual(
                cal_before["provenance"]["near_history_sha256"],
                cal_after["provenance"]["near_history_sha256"],
            )
            self.assertNotEqual(cal_before["run_id"], cal_after["run_id"])
            cat_after = service.catalog()
            self.assertIsNotNone(cat_after)
            self.assertEqual(cat_before["timeframe"], cat_after["timeframe"])

            # 4. Also verify carry.fetch_history's dedicated mtime/size cache
            #    observes a same-size in-place edit immediately.
            till_match = srh3_eod.name.split("-")[4:7]
            till_str = "-".join(till_match)
            before_hist = fetch_history(
                "SRH3",
                HISTORY_FROM,
                till_str,
                cache_dir=root / "data/spread-history-cache",
                delay_seconds=0,
                fields=HISTORY_FIELDS,
            )
            mutated_srh3_2 = mutated_srh3.replace("30001.0", "30002.0", 1)
            self.assertEqual(len(mutated_srh3), len(mutated_srh3_2))
            time.sleep(0.02)
            srh3_eod.write_text(mutated_srh3_2, encoding="utf-8")
            after_hist = fetch_history(
                "SRH3",
                HISTORY_FROM,
                till_str,
                cache_dir=root / "data/spread-history-cache",
                delay_seconds=0,
                fields=HISTORY_FIELDS,
            )
            self.assertNotEqual(before_hist["rows"][0], after_hist["rows"][0])

    def test_default_reference_eval_is_fast_while_direct_through_replay_stays_full(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOEX_PROOF_REFERENCE_EVAL", None)
            self.assertFalse(reference_eval())
        with patch.dict(os.environ, {"MOEX_PROOF_REFERENCE_EVAL": "1"}):
            self.assertTrue(reference_eval())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_h1_workspace(root)
            settings = ResearchSettings(
                timeframe="1h",
                validation_percent=20,
                allow_failed_volume=True,
                allow_failed_capital=True,
                initial_capital_rub=100000.0,
                # Fixed seed: this test compares two runs, which must stay comparable.
                random_seed=41,
            )
            service = ProofService(root)
            pair_req = {
                "universe": PAIR_UNIVERSE,
                "pair": "CNYRUBF/CRZ6",
                "strategy": "mean_reversion",
                "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                "fill": "next_open",
                "sizing": AUTO_SIZING,
            }
            cal_req = {
                "universe": CALENDAR_UNIVERSE,
                "asset": "SR",
                "pair": "SRH3/SRM3",
                "strategy": "zscore_mean_reversion",
                "parameters": {"window": 10, "entry_z": 1.2, "exit_z": 0.3},
                "fill": "next_open",
                "sizing": AUTO_SIZING,
            }
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MOEX_PROOF_REFERENCE_EVAL", None)
                pair_replay = service.prepare(pair_req, settings=settings)(
                    pair_req, through="2026-01-20"
                )
                self.assertIn("session_date", pair_replay["series"][0])
                self.assertIn("free_cash_rub", pair_replay["ledger"][0])
                self.assertIn("run_id", pair_replay)
                self.assertIn("provenance", pair_replay)

                cal_replay = service.prepare(cal_req, settings=settings)(
                    cal_req, through="2023-03-01"
                )
                self.assertIn("session_date", cal_replay["series"][0])
                self.assertIn("free_cash_rub", cal_replay["ledger"][0])
                self.assertIn("run_id", cal_replay)

    def test_spawn_pool_matches_sequential_bit_for_bit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_pool_test_workspace(root)
            targets = [
                {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"},
                {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRM6"},
                {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRU6"},
                {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"},
            ]
            batch_req = {
                "universe": PAIR_UNIVERSE,
                "pair": "CNYRUBF/CRZ6",
                "strategy": "mean_reversion",
                "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                "fill": "next_open",
                "sizing": AUTO_SIZING,
                "targets": targets,
            }
            strat_req = {**batch_req, "optimize_strategy": True}
            win_req = {
                "requests": [
                    {
                        "universe": PAIR_UNIVERSE,
                        "pair": "CNYRUBF/CRZ6",
                        "strategy": "mean_reversion",
                        "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                        "fill": "next_open",
                        "sizing": AUTO_SIZING,
                    },
                    {
                        "universe": PAIR_UNIVERSE,
                        "pair": "CNYRUBF/CRM6",
                        "strategy": "mean_reversion",
                        "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                        "fill": "next_open",
                        "sizing": AUTO_SIZING,
                    },
                    {
                        "universe": PAIR_UNIVERSE,
                        "pair": "CNYRUBF/CRU6",
                        "strategy": "mean_reversion",
                        "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                        "fill": "next_open",
                        "sizing": AUTO_SIZING,
                    },
                    {
                        "universe": CALENDAR_UNIVERSE,
                        "asset": "SR",
                        "pair": "SRH3/SRM3",
                        "strategy": "zscore_mean_reversion",
                        "parameters": {"window": 10, "entry_z": 1.2, "exit_z": 0.3},
                        "fill": "next_open",
                        "sizing": AUTO_SIZING,
                    },
                ]
            }

            def run_mode(workers: str, force: str):
                with patch.dict(
                    os.environ,
                    {"MOEX_PROOF_WORKERS": workers, "MOEX_PROOF_FORCE_POOL": force},
                ):
                    svc = ProofService(root)
                    try:
                        return {
                            "batch": research_run(svc, batch_req, optimize=True),
                            "strategy": research_run(svc, strat_req, optimize=True),
                            "plateau": checked_plateau(svc, batch_req),
                            "windows": window_summaries(svc, win_req),
                        }
                    finally:
                        svc.close()

            seq = run_mode("1", "0")
            par = run_mode("3", "1")
            self.assertEqual(seq["batch"]["status"], "maximum_found")
            self.assertEqual(seq["batch"]["group"]["member_count"], 4)
            self.assertEqual(_canon_sha(par), _canon_sha(seq))
            self.assertEqual(par, seq)

    def test_spawn_pool_cooperative_cancellation_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_pool_test_workspace(root)
            req = {
                "universe": PAIR_UNIVERSE,
                "pair": "CNYRUBF/CRZ6",
                "strategy": "mean_reversion",
                "parameters": {"lookback": 10, "entry_z": 1.2, "exit_z": 0.3},
                "fill": "next_open",
                "sizing": AUTO_SIZING,
                "targets": [
                    {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"},
                    {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRM6"},
                    {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRU6"},
                    {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"},
                ],
            }
            with patch.dict(
                os.environ,
                {"MOEX_PROOF_WORKERS": "3", "MOEX_PROOF_FORCE_POOL": "1"},
            ):
                svc = ProofService(root)
                try:
                    calls = 0

                    def cancel_soon():
                        nonlocal calls
                        calls += 1
                        if calls >= 12:
                            raise ConnectionAbortedError("client disconnected")

                    with self.assertRaises(ConnectionAbortedError):
                        research_run(svc, req, optimize=True, check_cancelled=cancel_soon)

                    # Pool remains healthy and produces the exact canonical result after cancellation.
                    recovered = research_run(svc, req, optimize=True)
                    self.assertIn("run_id", recovered)
                    self.assertEqual(recovered["status"], "maximum_found")
                    self.assertEqual(recovered["group"]["member_count"], 4)
                finally:
                    svc.close()

    def test_fastcore_c_matches_python_fallback_bit_for_bit(self):
        from moex_spread_lab._fastcore_loader import load_fastcore
        from moex_spread_lab.analytics import compute_rolling_z, run_mean_reversion_baseline
        from moex_spread_lab.proof import _clean

        fc = load_fastcore()
        self.assertIsNotNone(fc, "_fastcore extension should build and load when compiler is available")

        # 1) compute_rolling_z including constant windows and floating-point sums
        ticks = [float(i % 7 - 3) + 0.125 * (i % 5) for i in range(120)]
        ticks[40:55] = [10.5] * 15
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOEX_PROOF_DISABLE_C", None)
            z_c = compute_rolling_z(ticks, 12, 1)
        with patch.dict(os.environ, {"MOEX_PROOF_DISABLE_C": "1"}, clear=False):
            self.assertIsNone(load_fastcore())
            z_py = compute_rolling_z(ticks, 12, 1)
        self.assertEqual(z_c, z_py)

        # 2) _clean / clean_obj preserving -0.0 and turning non-finite floats into None
        sample = {
            "ok": 1.5,
            "neg_zero": -0.0,
            "bad": [float("nan"), float("inf"), float("-inf"), (1, float("nan"), {"x": -0.0})],
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOEX_PROOF_DISABLE_C", None)
            clean_c = _clean(sample)
        with patch.dict(os.environ, {"MOEX_PROOF_DISABLE_C": "1"}, clear=False):
            clean_py = _clean(sample)
        self.assertEqual(_canon(clean_c), _canon(clean_py))
        self.assertEqual(math.copysign(1.0, clean_c["neg_zero"]), -1.0)

        # 3) run_mean_reversion_baseline(sizing_intents=True, metrics_only=True) with funding + forced exit
        rows = []
        funding = {}
        for i in range(40):
            d = f"2026-01-{i + 1:02d}" if i < 31 else f"2026-02-{i - 30:02d}"
            px = 10.0 + (2.5 if i >= 30 else 0.1 * (i % 4))
            rows.append(
                {
                    "date": d,
                    "timestamp": f"{d}T10:00:00",
                    "near_open": px,
                    "near_close": px + 0.05,
                    "far_open": px + 1.0,
                    "far_close": px + 1.05,
                    "spread_ticks": float((i % 9) - 4 if i < 35 else 20),
                    "benchmark_price": 100.0 + i * 0.05,
                    "benchmark_source_kind": "test",
                }
            )
            funding[d] = 0.002
        series_obj = {
            "near": "CRZ6",
            "far": "CRM6",
            "contracts": {
                "CRZ6": {"initial_margin": 2500.0, "tick_value": 1.0, "tick_size": 1.0, "lot_size": 1.0},
                "CRM6": {"initial_margin": 2500.0, "tick_value": 1.0, "tick_size": 1.0, "lot_size": 1.0},
            },
            "rows": rows,
        }
        z_vals = compute_rolling_z([r["spread_ticks"] for r in rows], 8, 1)
        kw = dict(
            lookback=8,
            entry_z=1.0,
            exit_z=0.3,
            initial_capital=100000.0,
            decision_delay_bars=1,
            analysis_role="diagnostic_only",
            fill="open",
            funding_swaprates=funding,
            sizing_intents=True,
            metrics_only=True,
            precomputed_z=z_vals,
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MOEX_PROOF_DISABLE_C", None)
            rep_c = run_mean_reversion_baseline(series_obj, **kw)
        with patch.dict(os.environ, {"MOEX_PROOF_DISABLE_C": "1"}, clear=False):
            rep_py = run_mean_reversion_baseline(series_obj, **kw)
        self.assertEqual(_canon(rep_c), _canon(rep_py))

        # 4) End-to-end pool cohort + plateau + windows with C enabled vs MOEX_PROOF_DISABLE_C=1
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_pool_test_workspace(root)
            targets = [
                {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"},
                {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRM6"},
                {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRU6"},
            ]
            req = {
                "universe": PAIR_UNIVERSE,
                "pair": "CNYRUBF/CRZ6",
                "strategy": "mean_reversion",
                "fill": "next_open",
                "sizing": AUTO_SIZING,
                "optimize_strategy": True,
                "targets": targets,
            }

            def run_with_c(disable_c: str):
                env = {"MOEX_PROOF_WORKERS": "2", "MOEX_PROOF_FORCE_POOL": "1"}
                if disable_c:
                    env["MOEX_PROOF_DISABLE_C"] = disable_c
                with patch.dict(os.environ, env, clear=False):
                    if not disable_c:
                        os.environ.pop("MOEX_PROOF_DISABLE_C", None)
                    svc = ProofService(root)
                    try:
                        return research_run(svc, req, optimize=True)
                    finally:
                        svc.close()

            self.assertEqual(
                _canon(run_with_c("")),
                _canon(run_with_c("1")),
            )

            # 5) Calendar 1h strategy_search (zscore_mean_reversion + convergence_to_ruonia)
            #    C double-double sample stdev/mean + scalar_only sizing vs pure Python fallback
            cal_req = {
                "universe": CALENDAR_UNIVERSE,
                "asset": "SR",
                "pair": "SRH3/SRM3",
                "strategy": "zscore_mean_reversion",
                "fill": "next_open",
                "sizing": AUTO_SIZING,
                "optimize_strategy": True,
                "targets": [
                    {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"},
                    {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRM3/SRU3"},
                ],
            }

            def run_cal(disable_c: str, ref_eval: str = "0"):
                env = {"MOEX_PROOF_WORKERS": "1", "MOEX_PROOF_REFERENCE_EVAL": ref_eval}
                if disable_c:
                    env["MOEX_PROOF_DISABLE_C"] = disable_c
                with patch.dict(os.environ, env, clear=False):
                    if not disable_c:
                        os.environ.pop("MOEX_PROOF_DISABLE_C", None)
                    svc = ProofService(root)
                    try:
                        return research_run(svc, cal_req, optimize=True)
                    finally:
                        svc.close()

            cal_c_fast = _canon(run_cal("", "0"))
            cal_py_fast = _canon(run_cal("1", "0"))
            cal_py_ref = _canon(run_cal("1", "1"))
            self.assertEqual(cal_c_fast, cal_py_fast)
            self.assertEqual(cal_c_fast, cal_py_ref)

    def test_streaming_progress_is_monotonic_and_matches_json_bit_for_bit(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from moex_spread_lab.screener_app import ScreenerInputs, ScreenerStore, make_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _build_pool_test_workspace(root)
            store = ScreenerStore(ScreenerInputs(root))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"

            def post_json(endpoint: str, body: dict):
                req = urllib.request.Request(
                    f"{base_url}{endpoint}",
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            def post_stream(endpoint: str, body: dict):
                req = urllib.request.Request(
                    f"{base_url}{endpoint}",
                    data=json.dumps(body).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-Progress-Stream": "1",
                    },
                )
                events = []
                result = None
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self.assertIn("application/x-ndjson", resp.headers.get("Content-Type", ""))
                    for raw in resp:
                        line = raw.decode("utf-8").strip()
                        if not line:
                            continue
                        msg = json.loads(line)
                        if msg["type"] == "progress":
                            events.append(msg)
                        elif msg["type"] == "result":
                            result = msg["data"]
                        else:
                            self.fail(f"Unexpected stream message: {msg}")
                return events, result

            try:
                targets = [
                    {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"},
                    {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRM6"},
                    {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRU6"},
                ]
                run_body = {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                    "parameters": {"lookback": 12, "entry_z": 1.5, "exit_z": 0.4},
                    "include_plateau": True,
                }
                opt_body = {
                    "universe": PAIR_UNIVERSE,
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                    "optimize_strategy": True,
                    "targets": targets,
                }
                plat_body = {
                    "targets": targets,
                    "strategy": "mean_reversion",
                    "fill": "next_open",
                    "sizing": AUTO_SIZING,
                    "parameters": {"lookback": 12, "entry_z": 1.5, "exit_z": 0.4},
                }
                win_body = {
                    "requests": [
                        {
                            **t,
                            "strategy": "mean_reversion",
                            "fill": "next_open",
                            "sizing": AUTO_SIZING,
                            "parameters": {"lookback": 12, "entry_z": 1.5, "exit_z": 0.4},
                        }
                        for t in targets
                    ]
                }

                for endpoint, body, min_events in [
                    ("/api/proof/run", run_body, 3),
                    ("/api/proof/optimize", opt_body, 10),
                    ("/api/proof/plateau", plat_body, 2),
                    ("/api/proof/windows", win_body, 2),
                ]:
                    plain = post_json(endpoint, body)
                    events, streamed = post_stream(endpoint, body)
                    self.assertGreaterEqual(len(events), min_events, endpoint)
                    pcts = [float(ev["percent"]) for ev in events]
                    self.assertEqual(pcts, sorted(pcts), f"Non-monotonic progress on {endpoint}: {pcts}")
                    self.assertGreaterEqual(pcts[0], 0.0)
                    self.assertLessEqual(pcts[-1], 100.0)
                    for ev in events:
                        self.assertTrue(ev.get("stage"))
                        self.assertTrue(ev.get("label"))
                        self.assertGreaterEqual(ev["done"], 0)
                        self.assertGreaterEqual(ev["total"], ev["done"])
                    self.assertEqual(_canon(streamed), _canon(plain))

                ui_html = (Path(__file__).resolve().parents[3] / "src/moex_spread_lab/proof_ui.html").read_text(encoding="utf-8")
                self.assertNotIn("render-progress-run", ui_html)
                self.assertIn('aria-valuemin="0"', ui_html)
                self.assertIn('aria-valuemax="100"', ui_html)
                self.assertIn("apiStream", ui_html)
                self.assertIn("progress-pill", ui_html)
            finally:
                server.shutdown()
                server.server_close()
                store.close()


def _build_pool_test_workspace(root: Path) -> None:
    _build_h1_workspace(root)
    intake_path = root / "data/w1/current-intake.json"
    intake = json.loads(intake_path.read_text(encoding="utf-8"))
    crz6 = next(item for item in intake["instruments"] if item.get("secid") == "CRZ6")
    intake["instruments"] = [
        item for item in intake["instruments"] if item.get("secid") != "CRM6"
    ]
    intake["instruments"].append(
        {
            **crz6,
            "secid": "CRM6",
            "expiry": "2026-06-18",
            "settlement": 13.45,
            "tariff_group": "currency",
        }
    )
    intake["instruments"].append(
        {
            **crz6,
            "secid": "CRU6",
            "expiry": "2026-09-17",
            "settlement": 13.55,
            "tariff_group": "currency",
        }
    )
    intake_path.write_text(json.dumps(intake), encoding="utf-8")
    crz6_candles = root / "data/w1/candles-screen/CRZ6_1h.json"
    (root / "data/w1/candles-screen/CRU6_1h.json").write_text(
        crz6_candles.read_text(encoding="utf-8").replace("CRZ6", "CRU6"),
        encoding="utf-8",
    )
    save_settings(
        root,
        {
            "timeframe": "1h",
            "validation_percent": 20,
            "allow_failed_volume": True,
            "allow_failed_capital": True,
            "initial_capital_rub": 100000.0,
            # A fixed seed keeps two runs comparable; seed 0 would draw a new one.
            "random_seed": 41,
        },
    )


if __name__ == "__main__":
    unittest.main()
