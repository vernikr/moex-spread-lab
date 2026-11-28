"""Synthetic proof workspaces, request builders and isolated service lifecycles.

All equivalence tiers use identical fixture bytes and retain their own tests,
trial budgets and independent reference/fast evaluations.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing, contextmanager
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.settings import ResearchSettings, save_settings
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE, PAIR_UNIVERSE
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.service import ProofService
from moex_spread_lab.proof.strategy_catalog import (
    clear_strategy_registry_cache,
    get_strategy_registry,
)
from synthetic_history import iso_weekdays as _weekdays
from synthetic_history import write_h1_candles as _write_h1_candles
from workspace_fixtures import write_calendar_history

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


def _write_cal_eod(root, secid, days, row_fn):
    write_calendar_history(
        root, secid, [row_fn(i, d) for i, d in enumerate(days)], response_hash=f"sha-{secid}"
    )


def _build_h1_workspace(root: Path) -> None:
    """Create a rich 1h + 1d synthetic workspace with pair_screen and calendar_native windows.

    The same bytes as the single builder this replaces in ``test_perf_equivalence.py``;
    the steps are named so no single function carries the whole fixture.
    """
    from proof_fixtures import fixture

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


class H1WorkspaceCase(unittest.TestCase):
    """One synthetic 1h workspace per class plus reference-vs-fast comparison."""

    root: Path

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        _build_h1_workspace(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _compare_modes(self, settings: ResearchSettings, fn):
        results = []
        with patch.object(ResearchSettings, "load", return_value=settings):
            for mode in ("1", "0"):
                with (
                    patch.dict(os.environ, {"MOEX_PROOF_REFERENCE_EVAL": mode}),
                    closing(ProofService(self.root)) as service,
                ):
                    results.append(fn(service))
        expected, actual = results
        self.assertEqual(_canon_sha(actual), _canon_sha(expected))
        self.assertEqual(actual, expected)
        return actual


def _build_pool_test_workspace(root: Path) -> None:
    _build_h1_workspace(root)
    intake_path = root / "data/w1/current-intake.json"
    intake = json.loads(intake_path.read_text(encoding="utf-8"))
    crz6 = next(item for item in intake["instruments"] if item.get("secid") == "CRZ6")
    intake["instruments"] = [item for item in intake["instruments"] if item.get("secid") != "CRM6"]
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


@contextmanager
def installed_plugin(tmp_path, monkeypatch, module, source, entries):
    """Real entry-point discovery, including spawned workers, without process leakage."""
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / f"{module}.py").write_text(source)
    dist = plugin_dir / f"{module}-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {module}\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text(
        "[moex_spread_lab.strategies]\n"
        + "".join(f"{sid} = {module}:{attr}\n" for sid, attr in entries.items())
    )
    with monkeypatch.context() as isolated, ExitStack() as services:
        isolated.syspath_prepend(str(plugin_dir))
        isolated.setenv("MOEX_OFFLINE_ONLY", "1")
        isolated.setenv("MOEX_PROOF_WORKERS", "1")
        isolated.delenv("MOEX_PROOF_FORCE_POOL", raising=False)
        clear_strategy_registry_cache()

        def service(root):
            instance = ProofService(root)
            services.callback(instance.close)
            return instance

        try:
            assert set(entries) <= set(get_strategy_registry().ids())
            yield service
        finally:
            # Close pools before removing entry points from the parent and workers.
            services.close()
            sys.modules.pop(module, None)
            clear_strategy_registry_cache()
    assert not set(entries) & set(get_strategy_registry().ids())


def assert_pool_parity(root, request, pairs, service_factory, monkeypatch):
    cohort = {**request, "targets": [{"universe": "pair_screen", "pair": pair} for pair in pairs]}
    with monkeypatch.context() as workers:
        workers.setenv("MOEX_PROOF_WORKERS", "1")
        serial = research_run(service_factory(root), cohort, optimize=True)
        workers.setenv("MOEX_PROOF_WORKERS", "2")
        workers.setenv("MOEX_PROOF_FORCE_POOL", "1")
        parallel_service = service_factory(root)
        parallel = research_run(parallel_service, cohort, optimize=True)
        assert len(parallel_service._pool._worker_slots) == 2
        assert serial["selected_parameters"] is not None
        assert serial["selected_parameters"] == parallel["selected_parameters"]
        assert serial["run_id"] == parallel["run_id"]
        parallel_service.close()


def temporary_workspace(case, build):
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    build(root)
    return root


class ProofWorkspaceCase(unittest.TestCase):
    def setUp(self):
        from proof_fixtures import two_calendar_windows

        self.root = temporary_workspace(self, two_calendar_windows)
        self.service = ProofService(self.root)
        self.addCleanup(self.service.close)

    def run_proof(self, request):
        return research_run(self.service, request, optimize=False, include_plateau=False)


def pair_screen_request(**overrides: Any) -> dict[str, Any]:
    """Pair-screen ``mean_reversion`` request with automatic sizing."""
    request: dict[str, Any] = {
        "universe": PAIR_UNIVERSE,
        "pair": "CNYRUBF/CRZ6",
        "strategy": "mean_reversion",
        "fill": "next_open",
        "sizing": AUTO_SIZING,
    }
    request.update(overrides)
    return request


def calendar_spread_request(**overrides: Any) -> dict[str, Any]:
    """Calendar ``zscore_mean_reversion`` request with per-case overrides."""
    request: dict[str, Any] = {
        "universe": CALENDAR_UNIVERSE,
        "asset": "SR",
        "pair": "SRH3/SRM3",
        "strategy": "zscore_mean_reversion",
        "fill": "next_open",
        "sizing": AUTO_SIZING,
    }
    request.update(overrides)
    return request


def pool_targets(*, calendar=True):
    targets = [
        {"universe": PAIR_UNIVERSE, "pair": f"CNYRUBF/{far}"} for far in ("CRZ6", "CRM6", "CRU6")
    ]
    if calendar:
        targets.append({"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "SRH3/SRM3"})
    return targets


def search_with_c(root, request, disable_c, *, workers, reference="0", force="0"):
    with patch.dict(
        os.environ,
        {
            "MOEX_PROOF_WORKERS": workers,
            "MOEX_PROOF_FORCE_POOL": force,
            "MOEX_PROOF_REFERENCE_EVAL": reference,
            "MOEX_PROOF_DISABLE_C": disable_c,
        },
    ):
        service = ProofService(root)
        try:
            return research_run(service, request, optimize=True)
        finally:
            service.close()


def pair_screen_fixed_quantity_request(**overrides: Any) -> dict[str, Any]:
    """Pair-screen request with a manual quantity and no automatic ``sizing`` key."""
    request: dict[str, Any] = {
        "universe": PAIR_UNIVERSE,
        "pair": "CNYRUBF/CRZ6",
        "strategy": "mean_reversion",
        "fill": "next_open",
        "quantity": 1,
        "slippage_ticks": 1.0,
    }
    request.update(overrides)
    return request


def convergence_window_requests(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One ``convergence_to_ruonia`` window request per calendar target."""
    return [
        {
            "universe": CALENDAR_UNIVERSE,
            "asset": "SR",
            "pair": target["pair"],
            "strategy": "convergence_to_ruonia",
            "parameters": {"threshold_pp": 0.5},
            "fill": "next_open",
            "sizing": AUTO_SIZING,
        }
        for target in targets
    ]


def assert_same_fields(actual, expected, *fields):
    for field in fields:
        assert actual[field] == expected[field], (field, actual[field], expected[field])
