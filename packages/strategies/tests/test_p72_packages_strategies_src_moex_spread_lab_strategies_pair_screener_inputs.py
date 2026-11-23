"""Regression coverage for the strict typing of ``pair_screener.inputs`` (Issue #115).

The change under test is typing-only: typed aliases for the universe registry, an
explicit ``__all__`` for the facade hooks, a typed history adapter instead of a lambda,
and an invariant cast instead of a suppression comment. These tests pin the runtime
behaviour those annotations describe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from moex_spread_lab.core import universe
from moex_spread_lab.strategies.pair_screener import inputs as inputs_module
from moex_spread_lab.strategies.pair_screener.inputs import (
    HISTORY_WINDOWS,
    PAIRS,
    ScreenerError,
    ScreenerInputs,
    _candle_summary,
    _leg_volumes,
    _make_proof_funding,
    _offline_only,
    _pair,
    _pair_history_span,
    _parse_row_volume,
    _positive_leg_volume_dates,
    _session_volumes,
    pair_price_inputs,
)

NEAR = "CNYRUBF"
FAR = "CRZ6"
PAIR_ID = f"{NEAR}/{FAR}"
DAYS = [f"2026-01-0{index}" for index in range(1, 6)]
NEAR_VOLUMES = [5000, 4000, 3000, 0, 2000]
FAR_VOLUMES = [4500, 3500, 2500, 1000, 1500]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _build_root(root: Path) -> None:
    _write_json(
        root / "data/w1/current-intake.json",
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
                for secid in (NEAR, FAR)
            ],
        },
    )
    _write_json(
        root / "data/benchmark/lqdt-lineage.json",
        {
            "schema_version": "lqdt-lineage.v1",
            "rows": [
                {
                    "date": day,
                    "price": 1 + index * 0.0004,
                    "source_kind": "observed",
                    "source_ticker": "LQDT",
                }
                for index, day in enumerate(DAYS)
            ],
        },
    )
    for secid, base, volumes in ((NEAR, 12.0, NEAR_VOLUMES), (FAR, 13.1, FAR_VOLUMES)):
        _write_json(
            root / f"data/w1/candles/{secid}_1d.json",
            {
                "schema_version": "iss-candles.v1",
                "secid": secid,
                "interval": 24,
                "rows": [
                    {
                        "begin": f"{day} 00:00:00",
                        "open": base + index,
                        "close": base + index,
                        "high": base + index + 0.1,
                        "low": base + index - 0.1,
                        "volume": volume,
                        "price_proxy_kind": "candle_close",
                    }
                    for index, (day, volume) in enumerate(zip(DAYS, volumes, strict=True))
                ],
            },
        )


def _inputs(root: Path) -> ScreenerInputs:
    return ScreenerInputs(root)


def test_registry_aliases_are_the_core_objects() -> None:
    assert HISTORY_WINDOWS is universe.HISTORY_WINDOWS
    assert PAIRS is universe.PAIRS
    assert PAIRS
    assert {pair["pair_id"] for pair in PAIRS} <= set(HISTORY_WINDOWS)


def test_facade_all_declares_the_private_hooks() -> None:
    assert {
        "_leg_volumes",
        "_make_proof_funding",
        "_pair",
        "_pair_history_span",
    } <= set(inputs_module.__all__)


def test_pair_lookup_and_rejection() -> None:
    pair = _pair(PAIR_ID)
    assert pair["near"] == NEAR
    assert pair["far"] == FAR
    assert pair["family"] == "perpetual_quarterly_same_asset"
    with pytest.raises(ScreenerError, match="не входит"):
        _pair("MISSING/PAIR")


def test_input_paths_and_history_cache_folders(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    assert inputs.root == tmp_path.resolve()
    assert inputs.benchmark == inputs.root / "data/benchmark/lqdt-lineage.json"
    assert inputs.contracts == inputs.root / "data/w1/current-intake.json"
    assert inputs.history == inputs.root / "data/analytics/screener-runs.jsonl"
    assert inputs.screen_history == inputs.root / "data/analytics/screener-screens.jsonl"
    assert inputs.search_history == inputs.root / "data/analytics/screener-searches.jsonl"
    assert inputs.history_cache(PAIR_ID) == inputs.root / "data/carry-history-cache"
    assert inputs.history_cache("GLDRUBF/GLZ6") == inputs.root / "data/carry-screen-history-cache"
    assert inputs.candles(PAIR_ID, NEAR) == inputs.root / f"data/w1/candles/{NEAR}_1d.json"


def test_parse_row_volume_marks_invalid_values() -> None:
    assert _parse_row_volume({"begin": "2026-01-01 00:00:00", "volume": 10}) == (
        "2026-01-01",
        10.0,
        False,
    )
    assert _parse_row_volume({"begin": "2026-01-01 00:00:00", "volume": None}) == (
        "2026-01-01",
        None,
        True,
    )
    assert _parse_row_volume({"begin": "2026-01-01 00:00:00", "volume": True}) == (
        "2026-01-01",
        None,
        True,
    )
    assert _parse_row_volume({"begin": "2026-01-01 00:00:00", "volume": -1}) == (
        "2026-01-01",
        None,
        True,
    )
    assert _parse_row_volume({"begin": "short", "volume": 10}) == (None, None, False)


def test_session_volumes_sum_daily_rows_and_keep_invalid_days(tmp_path: Path) -> None:
    _build_root(tmp_path)
    pair = _pair(PAIR_ID)
    inputs = _inputs(tmp_path)
    summary = _candle_summary(inputs.bars(PAIR_ID, NEAR, columns=("begin", "close", "volume")))
    assert summary["volumes"][DAYS[0]] == 5000.0
    assert summary["volumes"][DAYS[3]] == 0.0
    assert set(summary["timestamps"]) == {f"{day} 00:00:00" for day in DAYS}
    legs = _session_volumes(inputs, pair)
    assert legs is not None
    assert set(legs) == {NEAR, FAR}
    assert legs[NEAR] == dict(zip(DAYS, (5000.0, 4000.0, 3000.0, 0.0, 2000.0), strict=True))
    assert legs[FAR][DAYS[4]] == 1500.0


def test_leg_volumes_returns_minimum_over_eligible_days(tmp_path: Path) -> None:
    _build_root(tmp_path)
    pair = _pair(PAIR_ID)
    inputs = _inputs(tmp_path)
    # The zero-volume day is not positive on both legs and is excluded from the minimum.
    assert _leg_volumes(inputs, pair) == {NEAR: 2000.0, FAR: 1500.0}
    assert _positive_leg_volume_dates(inputs, pair) == {DAYS[0], DAYS[1], DAYS[2], DAYS[4]}


def test_pair_history_span_uses_common_timestamps(tmp_path: Path) -> None:
    _build_root(tmp_path)
    pair = _pair(PAIR_ID)
    inputs = _inputs(tmp_path)
    start, end, count, days = _pair_history_span(inputs, pair)
    assert start == DAYS[0]
    assert end == DAYS[-1]
    assert count == len(DAYS)
    assert days == set(DAYS)


def test_offline_only_guard() -> None:
    with pytest.raises(OSError, match="does not download"):
        _offline_only("https://iss.moex.com")


def test_make_proof_funding_wires_injected_fetch(tmp_path: Path) -> None:
    _build_root(tmp_path)
    inputs = _inputs(tmp_path)
    calls: list[dict[str, Any]] = []

    def fetch_fn(secid: str, start: str, end: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"secid": secid, "start": start, "end": end, **kwargs})
        return {"rows": [{"TRADEDATE": DAYS[0]}]}

    funding = _make_proof_funding(inputs, fetch_fn=fetch_fn)
    source = funding.source(PAIR_ID, NEAR)
    assert source["rows"] == [{"TRADEDATE": DAYS[0]}]
    assert len(calls) == 1
    call = calls[0]
    assert call["secid"] == NEAR
    assert (call["start"], call["end"]) == HISTORY_WINDOWS[PAIR_ID]
    assert call["cache_dir"] == inputs.history_cache(PAIR_ID)
    assert call["delay_seconds"] == 0
    with pytest.raises(OSError):
        call["opener"]("https://iss.moex.com")
    # Request-local source cache still short-circuits the second lookup.
    assert funding.source(PAIR_ID, NEAR) is source
    assert len(calls) == 1


def test_pair_price_inputs_end_to_end(tmp_path: Path) -> None:
    _build_root(tmp_path)
    inputs = _inputs(tmp_path)
    pair = _pair(PAIR_ID)
    funding = _make_proof_funding(inputs, fetch_fn=lambda *_args, **_kwargs: {"rows": []})
    series, book = pair_price_inputs(inputs, pair, funding)
    assert series["schema_version"] == "analytical-series.v1"
    assert series["near"] == NEAR
    assert series["far"] == FAR
    assert series["timeframe"] == "1d"
    assert len(series["rows"]) == len(DAYS)
    assert book is not None
