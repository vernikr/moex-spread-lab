"""Synthetic plugin-owned fixtures shared by the strategy conformance suites.

Each builder returns a ``WindowSnapshot`` plus the chart series the plugin's
``compute_entry_bands`` consumes, so causal-truncation checks can compare both
halves of the conformance contract on the same dates.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from moex_spread_lab.core.contracts import WindowSnapshot
from moex_spread_lab.strategies.funding_carry.carry import build_carry_study

CONFORMANCE_CUTOFFS = (14, 20, 26)

PAIR_PARAMS = {"lookback": 6, "entry_z": 1.5, "exit_z": 0.5}
FUNDING_PARAMS = {"lookback": 5}
ZSCORE_PARAMS = {"window": 6, "entry_z": 1.5, "exit_z": 0.5}
RUONIA_PARAMS = {"threshold_pp": 1.0}


def pair_mean_reversion_fixture() -> tuple[WindowSnapshot, list[dict]]:
    start = date(2026, 1, 5)
    rows = []
    series_rows = []
    offsets = [
        0.0,
        2.0,
        -2.0,
        3.0,
        -1.0,
        1.5,
        -2.5,
        0.5,
        18.0,
        16.0,
        2.0,
        0.0,
        -1.0,
        -19.0,
        -15.0,
        -1.0,
        0.5,
        1.0,
        -0.5,
        0.0,
        2.0,
        -1.5,
        17.5,
        3.0,
        0.2,
        -0.4,
        0.8,
        -0.2,
        0.1,
        0.0,
    ]
    for i, off in enumerate(offsets):
        d = (start + timedelta(days=i)).isoformat()
        near_px = 1000.0 + off
        far_px = 1000.0
        rows.append(
            {
                "date": d,
                "timestamp": f"{d} 00:00:00",
                "trade_date": d,
                "near_open": near_px,
                "near_high": near_px + 2.0,
                "near_low": near_px - 2.0,
                "near_close": near_px,
                "near_volume": 2500.0,
                "far_open": far_px,
                "far_high": far_px + 2.0,
                "far_low": far_px - 2.0,
                "far_close": far_px,
                "far_volume": 2500.0,
                "near_value_rub": near_px,
                "far_value_rub": far_px,
                "spread_value": off,
                "spread_ticks": off,
                "spread_mid": off,
                "benchmark_date": d,
                "benchmark_price": 1.5 + i * 0.0005,
                "benchmark_source_ticker": "LQDT",
                "benchmark_source_kind": "observed",
                "benchmark_mark_kind": "close",
                "near_price_proxy_kind": "close",
                "far_price_proxy_kind": "close",
                "benchmark_daily_return": 0.0005 if i else 0.0,
            }
        )
        series_rows.append(
            {
                "date": d,
                "spread": off,
                "far": far_px,
                "volume_eligible": True,
            }
        )
    contracts = {
        "NEAR": {
            "secid": "NEAR",
            "lot_size": 1,
            "lot_volume": 1,
            "step_price": 1.0,
            "min_step": 1.0,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "initial_margin": 5000.0,
            "tariff_group": "currency",
        },
        "FAR": {
            "secid": "FAR",
            "lot_size": 1,
            "lot_volume": 1,
            "step_price": 1.0,
            "min_step": 1.0,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "initial_margin": 5000.0,
            "tariff_group": "currency",
        },
    }
    snap = WindowSnapshot(
        universe="pair_screen",
        pair="NEAR_FAR",
        timeframe="1d",
        rows=tuple(rows),
        contracts=contracts,
        near="NEAR",
        far="FAR",
    )
    return snap, series_rows


def _carry_instrument(secid: str, stable_id: str, expiry: str) -> dict:
    return {
        "secid": secid,
        "stable_id": stable_id,
        "board": "RFUD",
        "expiry": expiry,
        "lot_size": 1000,
        "tick_size": 0.001,
        "tick_value": 1.0,
        "initial_margin": 2000.0,
        "tariff_group": "currency",
        "source_vintage": "current_snapshot",
    }


def _iss_quote_row(day: str, secid: str, settle: float, rate: float | None) -> dict:
    return {
        "TRADEDATE": day,
        "SECID": secid,
        "SETTLEPRICE": settle,
        "OPEN": settle,
        "HIGH": round(settle + 0.05, 6),
        "LOW": round(settle - 0.05, 6),
        "CLOSE": settle,
        "VOLUME": 5000,
        "OPENPOSITION": 10000,
        "SWAPRATE": rate,
    }


def funding_carry_fixture(tmp_dir: Path) -> tuple[WindowSnapshot, list[dict]]:
    start = date(2026, 2, 1)
    rates = [2.5] * 10 + [-3.0] * 10 + [3.0] * 10
    near_rows = []
    far_rows = []
    bench_rows = []
    series_rows = []
    for i, rate in enumerate(rates):
        d = (start + timedelta(days=i)).isoformat()
        near_rows.append(_iss_quote_row(d, "CNYRUBF", round(13.50 + i * 0.01, 6), rate))
        far_rows.append(_iss_quote_row(d, "CRH6", round(13.80 + i * 0.01, 6), None))
        bench_rows.append(
            {
                "date": d,
                "price": round(1.50 + i * 0.0005, 6),
                "source_kind": "observed",
                "source_ticker": "LQDT",
            }
        )
        series_rows.append({"date": d, "spread": -300.0, "far": 13.80, "volume_eligible": True})

    contracts_path = tmp_dir / "contracts.json"
    contracts_path.write_text(
        json.dumps(
            {
                "instruments": [
                    _carry_instrument("CNYRUBF", "cny-perp", "2100-01-01"),
                    _carry_instrument("CRH6", "cny-far", "2026-06-18"),
                ]
            }
        ),
        encoding="utf-8",
    )
    benchmark_path = tmp_dir / "benchmark.json"
    benchmark_path.write_text(
        json.dumps({"schema_version": "lqdt-lineage.v1", "rows": bench_rows}),
        encoding="utf-8",
    )
    study = build_carry_study(
        {
            "schema_version": "iss-history.v1",
            "secid": "CNYRUBF",
            "rows": near_rows,
            "source": {"raw_response_sha256": ["near-hash"]},
        },
        {
            "schema_version": "iss-history.v1",
            "secid": "CRH6",
            "rows": far_rows,
            "source": {"raw_response_sha256": ["far-hash"]},
        },
        contracts_path,
        near="CNYRUBF",
        far="CRH6",
        funding_regimes=(),
    )
    snap = WindowSnapshot(
        universe="pair_screen",
        pair="CNYRUBF_CRH6",
        timeframe="1d",
        rows=tuple(study["rows"]),
        carry_study=study,
        benchmark=benchmark_path,
        near="CNYRUBF",
        far="CRH6",
        extra={"contracts_path": contracts_path},
    )
    return snap, series_rows


CALENDAR_SPREADS = (
    1200.0,
    1210.0,
    1195.0,
    1205.0,
    1198.0,
    1202.0,
    1207.0,
    1193.0,
    1201.0,
    1199.0,
    1550.0,
    1540.0,
    1202.0,
    1200.0,
    860.0,
    865.0,
    1200.0,
    1201.0,
    1199.0,
    1200.0,
    1204.0,
    1196.0,
    1520.0,
    1203.0,
    1200.0,
    1198.0,
    1202.0,
    1197.0,
    1200.0,
    1200.0,
)
# Near-expiry buffer exit arrives around bar 18, which the RUONIA convergence rules need.
CALENDAR_DTES = (
    45,
    44,
    43,
    42,
    41,
    40,
    39,
    38,
    37,
    36,
    25,
    24,
    23,
    22,
    10,
    2,
    20,
    19,
    18,
    17,
    16,
    15,
    14,
    13,
    12,
    2,
    10,
    9,
    8,
    7,
)


def calendar_fixture() -> tuple[WindowSnapshot, list[dict]]:
    start = date(2026, 1, 10)
    rows = []
    series_rows = []
    for i, (spread, dte) in enumerate(zip(CALENDAR_SPREADS, CALENDAR_DTES, strict=True)):
        d = (start + timedelta(days=i)).isoformat()
        near_val = 30_000.0
        far_val = near_val + spread
        implied = spread / near_val * 365.0 / 91.0
        dev_pp = (implied - 0.16) * 100.0
        scale = near_val * 91.0 / 36500.0
        rows.append(
            {
                "date": d,
                "spread": spread,
                "open_spread": spread,
                "near_close": near_val,
                "far_close": far_val,
                "dev_pp": dev_pp,
                "days_to_near_expiry": dte,
                "dividend_in_window": False,
                "margin": 6000.0,
                "min_volume": 2500.0,
                "signal_eligible": True,
                "entry_eligible": True,
                "exit_eligible": True,
            }
        )
        series_rows.append(
            {
                "date": d,
                "spread": spread,
                "far": far_val,
                "point_value_rub": 1.0,
                "dev_pp": dev_pp,
                "rate_price_per_pp": scale,
                "volume_eligible": True,
            }
        )
    snap = WindowSnapshot(
        universe="calendar_native",
        pair="SRH6/SRM6",
        asset="SR",
        timeframe="1d",
        rows=tuple(rows),
        extra={"monthly": False, "convergence": True},
    )
    return snap, series_rows


BUILTIN_PARAMS = {
    "mean_reversion": PAIR_PARAMS,
    "funding_only": FUNDING_PARAMS,
    "zscore_mean_reversion": ZSCORE_PARAMS,
    "convergence_to_ruonia": RUONIA_PARAMS,
}


def builtin_fixture(strategy_id: str, tmp_dir: Path) -> tuple[WindowSnapshot, list[dict]]:
    """Fixture that a built-in plugin's conformance expectations are written against."""
    if strategy_id == "mean_reversion":
        return pair_mean_reversion_fixture()
    if strategy_id == "funding_only":
        return funding_carry_fixture(tmp_dir)
    return calendar_fixture()
