"""Regression pins for the carry benchmark JSON boundary and cost facade."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from moex_spread_lab.core.hashed_json_cache import HashedJSONCache
from moex_spread_lab.strategies.funding_carry import strategy_costs as costs
from moex_spread_lab.strategies.funding_carry.study import CarryStrategyError


def _write(tmp_path: Path, payload: Any) -> Path:
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_benchmark_marks_cache_and_digest(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "schema_version": "lqdt-lineage.v1",
            "rows": [
                {"date": "2026-01-01", "price": "100", "source_kind": "observed"},
                {"date": "2026-01-03", "price": 101},
                {"date": "2026-01-03", "price": 102, "source_ticker": "LQDT"},
            ],
        },
    )
    assert costs._HashedJSONCache is HashedJSONCache
    rows, digest = costs._benchmark_rows(path)
    assert costs._benchmark_rows(path)[0] is rows
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    lookup, first, lookup_digest = costs.make_benchmark_lookup(path, "2026-01-02")
    assert lookup_digest == digest
    assert first == {
        "date": "2026-01-01",
        "price": 100.0,
        "source_kind": "observed",
        "source_ticker": "unknown",
        "mark_kind": "prior_daily_close",
    }
    assert lookup("2026-01-03") == {
        "date": "2026-01-03",
        "price": 102.0,
        "source_kind": "unknown",
        "source_ticker": "LQDT",
        "mark_kind": "daily_close_same_date",
    }
    with pytest.raises(CarryStrategyError, match="no mark on or before"):
        lookup("2025-12-31")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "LQDT benchmark must be a JSON object with string keys"),
        ({"schema_version": "wrong"}, "unsupported LQDT benchmark schema"),
        ({"schema_version": "lqdt-lineage.v1"}, "empty LQDT benchmark"),
        ({"schema_version": "lqdt-lineage.v1", "rows": [1]}, "malformed LQDT benchmark row"),
        (
            {"schema_version": "lqdt-lineage.v1", "rows": [{"date": 1, "price": 2}]},
            "malformed LQDT benchmark row",
        ),
        (
            {"schema_version": "lqdt-lineage.v1", "rows": [{"date": "d"}]},
            "invalid LQDT benchmark price on d",
        ),
        (
            {"schema_version": "lqdt-lineage.v1", "rows": [{"date": "d", "price": 0}]},
            "LQDT benchmark price must be positive on d",
        ),
    ],
)
def test_benchmark_validation(tmp_path: Path, payload: Any, message: str) -> None:
    with pytest.raises(CarryStrategyError, match=message):
        costs._benchmark_rows(_write(tmp_path, payload))


def test_illustrative_costs_and_facade() -> None:
    near = {"tick_value": 2.0, "lot_size": 1000}
    far = {"tick_value": 3.0, "lot_size": 1000}
    profile = costs.resolve_carry_cost_profile("base", 0, near, far, {})
    assert profile == {
        "exchange": 1.0,
        "clearing": 1.0,
        "broker": 1.0,
        "slippage_ticks": 1.0,
        "exit": 1.0,
        "entry_cost": 11.0,
        "exit_cost": 13.0,
        "round_trip_cost": 24.0,
        "scenario": "base",
        "funding_component": "actual_SWAPRATE_included;_illustrative_funding_cost_not_added",
        "status": "illustrative_unverified",
    }
    with pytest.raises(CarryStrategyError, match="requires settlement prices"):
        costs.resolve_carry_cost_profile("tariff", 0, near, far, {})
