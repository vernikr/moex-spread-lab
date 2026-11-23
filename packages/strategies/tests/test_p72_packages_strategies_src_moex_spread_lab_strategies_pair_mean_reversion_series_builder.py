"""Isolated regressions for the strict-typed pair mean reversion series builder."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from moex_spread_lab.strategies.pair_mean_reversion import costs_helper, series_builder
from moex_spread_lab.strategies.pair_mean_reversion.analytics import (
    _calendar_days as analytics_calendar_days,
)
from moex_spread_lab.strategies.pair_mean_reversion.analytics import (
    _number as analytics_number,
)


def test_p72_packages_strategies_src_moex_spread_lab_strategies_pair_mean_reversion_series_builder_facade_identity() -> (
    None
):
    assert set(series_builder.__all__) <= set(vars(series_builder))
    # The strict-typed facade must keep the exact costs_helper objects so every
    # downstream consumer (analytics, report_builder) observes one implementation.
    assert series_builder._number is costs_helper._number
    assert series_builder._calendar_days is costs_helper._calendar_days
    assert series_builder._validate_cost_profile is costs_helper._validate_cost_profile
    assert series_builder._round is costs_helper.round_value
    assert series_builder._round is costs_helper._round
    assert series_builder._trade_cost_breakdown is costs_helper.trade_cost_breakdown
    assert series_builder._trade_cost_breakdown is costs_helper._trade_cost_breakdown
    # Re-export chain: analytics pulls the same objects through this facade.
    assert analytics_number is costs_helper._number
    assert analytics_calendar_days is costs_helper._calendar_days


def test_p72_packages_strategies_src_moex_spread_lab_strategies_pair_mean_reversion_series_builder_helper_contracts() -> (
    None
):
    assert series_builder._number(2, "field") == 2.0
    assert series_builder._number(1, "field", positive=True) == 1.0
    try:
        series_builder._number(0, "field", positive=True)
    except series_builder.AnalyticsError as exc:
        assert str(exc) == "field must be positive"
    else:
        raise AssertionError("non-positive value must fail")
    try:
        series_builder._number("x", "field")
    except series_builder.AnalyticsError as exc:
        assert str(exc) == "field must be a finite number"
    else:
        raise AssertionError("non-numeric value must fail")
    assert series_builder._calendar_days("2026-01-01T00:00:00", "2026-01-03T00:00:00") == 2
    assert series_builder._round(1.234567891234) == 1.23456789
    try:
        series_builder._validate_cost_profile({"schema_version": "other"})
    except series_builder.AnalyticsError as exc:
        assert str(exc) == "unsupported cost profile schema"
    else:
        raise AssertionError("foreign cost profile must fail")


def test_p72_packages_strategies_src_moex_spread_lab_strategies_pair_mean_reversion_series_builder_row_parsing() -> (
    None
):
    timestamp, row = series_builder._parse_candle_row(
        "NEAR",
        {
            "begin": "2026-01-01 00:00:00",
            "close": 10.5,
            "open": 10.0,
            "price_proxy_kind": "candle_close",
            "source": {"raw_response_sha256": "aa" * 32},
        },
    )
    assert timestamp == "2026-01-01 00:00:00"
    assert row == {
        "date": "2026-01-01",
        "timestamp": "2026-01-01 00:00:00",
        "open": 10.0,
        "close": 10.5,
        "source": {"raw_response_sha256": "aa" * 32},
        "price_proxy_kind": "candle_close",
    }
    # The isinstance guard must keep rejecting non-mapping rows after typing.
    try:
        series_builder._parse_candle_row("NEAR", ["not", "a", "mapping"])
    except series_builder.AnalyticsError as exc:
        assert str(exc) == "malformed candle row for NEAR"
    else:
        raise AssertionError("malformed row must fail")
    # Missing open falls back to the close proxy without changing the contract.
    _, openless = series_builder._parse_candle_row(
        "NEAR", {"begin": "2026-01-01 00:00:00", "close": 10.5, "price_proxy_kind": "candle_close"}
    )
    assert openless["open"] == 10.5


def test_p72_packages_strategies_src_moex_spread_lab_strategies_pair_mean_reversion_series_builder_build_series_contract() -> (
    None
):
    candle_payload = {
        "schema_version": "iss-candles.v1",
        "secid": "NEAR",
        "interval": 24,
        "rows": [
            {
                "begin": "2026-01-01 00:00:00",
                "close": 10.0,
                "price_proxy_kind": "candle_close",
                "source": {"raw_response_sha256": "aa" * 32},
            },
            {
                "begin": "2026-01-02 00:00:00",
                "close": 10.1,
                "price_proxy_kind": "candle_close",
                "source": {"raw_response_sha256": "bb" * 32},
            },
        ],
    }
    far_payload = json.loads(json.dumps(candle_payload))
    far_payload["secid"] = "FAR"
    far_payload["rows"][0]["close"] = 9.9
    far_payload["rows"][1]["close"] = 9.95
    benchmark_payload = {
        "schema_version": "lqdt-lineage.v1",
        "rows": [
            {
                "date": "2026-01-01",
                "price": 1.0,
                "source_ticker": "LQDT",
                "source_kind": "observed",
            },
            {
                "date": "2026-01-02",
                "price": 1.01,
                "source_ticker": "LQDT",
                "source_kind": "observed",
            },
        ],
    }
    contracts_payload = {
        "instruments": [
            {
                "secid": secid,
                "stable_id": stable_id,
                "board": "RFUD",
                "expiry": expiry,
                "lot_size": 1000,
                "tick_size": 0.001,
                "tick_value": 1,
                "initial_margin": 900,
                "source_vintage": "current_snapshot",
            }
            for secid, stable_id, expiry in (
                ("NEAR", "n", "2026-12-01"),
                ("FAR", "f", "2027-03-01"),
            )
        ]
    }
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "near.json").write_text(json.dumps(candle_payload), encoding="utf-8")
        (root / "far.json").write_text(json.dumps(far_payload), encoding="utf-8")
        (root / "benchmark.json").write_text(json.dumps(benchmark_payload), encoding="utf-8")
        (root / "contracts.json").write_text(json.dumps(contracts_payload), encoding="utf-8")
        series = series_builder.build_series(
            root / "near.json",
            root / "far.json",
            root / "benchmark.json",
            root / "contracts.json",
            near="NEAR",
            far="FAR",
        )
        benchmark_rows, benchmark_hash = series_builder._load_benchmark(root / "benchmark.json")

    assert series["schema_version"] == "analytical-series.v1"
    assert series["direction"] == "near_minus_far"
    assert [row["spread_ticks"] for row in series["rows"]] == [100.0, 150.0]
    assert series["rows"][1]["benchmark_mark_kind"] == "daily_close_same_date"
    assert set(series["provenance"]) == {
        "near_candles_sha256",
        "far_candles_sha256",
        "benchmark_sha256",
        "contracts_sha256",
        "near_candles_path",
        "far_candles_path",
        "benchmark_path",
        "contracts_path",
    }
    assert benchmark_hash == series["provenance"]["benchmark_sha256"]
    assert set(benchmark_rows) == {"2026-01-01", "2026-01-02"}
    assert benchmark_rows["2026-01-02"]["source_kind"] == "observed"


def test_p72_packages_strategies_src_moex_spread_lab_strategies_pair_mean_reversion_series_builder_rejects_malformed_benchmark_rows() -> (
    None
):
    payload: dict[str, Any] = {
        "schema_version": "lqdt-lineage.v1",
        "rows": [{"date": "2026-01-01", "price": 1.0}, "not-a-mapping"],
    }
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "benchmark.json").write_text(json.dumps(payload), encoding="utf-8")
        try:
            series_builder._load_benchmark(root / "benchmark.json")
        except series_builder.AnalyticsError as exc:
            assert str(exc) == "malformed LQDT benchmark row"
        else:
            raise AssertionError("malformed benchmark row must fail")
