"""Pair-screener runner validation and deterministic ``build_result`` outcomes.

Covers runtime type guards for ``selection`` and ``parameters``, parameter/execution range
validation, and stable run ids across mean-reversion and funding-only branches.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from moex_spread_lab.core.derived_cache import HashedJSONCache
from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.core.universe import MAX_QUANTITY
from moex_spread_lab.data import HISTORY_FIELDS, history_url
from moex_spread_lab.strategies.pair_screener import inputs as inputs_module
from moex_spread_lab.strategies.pair_screener import normalization as normalization_module
from moex_spread_lab.strategies.pair_screener.catalog import _pair_margin_metrics
from moex_spread_lab.strategies.pair_screener.inputs import (
    HISTORY_WINDOWS,
    ScreenerError,
    ScreenerInputs,
)
from moex_spread_lab.strategies.pair_screener.runner import (
    _parameters,
    build_result,
    execution_settings,
)


def _write_fixture(root: Path) -> list[date]:
    inputs = ScreenerInputs(root)
    inputs.contracts.parent.mkdir(parents=True, exist_ok=True)
    instruments = [
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
        for secid in ("CNYRUBF", "CRZ6", "GLDRUBF", "GLZ6")
    ]
    inputs.contracts.write_text(
        json.dumps({"schema_version": "iss-intake.v1", "instruments": instruments}),
        encoding="utf-8",
    )
    inputs.benchmark.parent.mkdir(parents=True, exist_ok=True)
    days: list[date] = []
    current = date(2026, 3, 23)
    while len(days) < 50:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    inputs.benchmark.write_text(
        json.dumps(
            {
                "schema_version": "lqdt-lineage.v1",
                "rows": [
                    {
                        "date": day.isoformat(),
                        "price": 1 + index * 0.0003,
                        "source_kind": "observed",
                        "source_ticker": "LQDT",
                    }
                    for index, day in enumerate(days)
                ],
            }
        ),
        encoding="utf-8",
    )
    for pair_id in ("CNYRUBF/CRZ6", "GLDRUBF/GLZ6"):
        near, far = pair_id.split("/")
        start, end = HISTORY_WINDOWS[pair_id]
        for secid, offset in ((near, 0), (far, 1)):
            candle_path = inputs.candles(pair_id, secid)
            candle_path.parent.mkdir(parents=True, exist_ok=True)
            candle_path.write_text(
                json.dumps(
                    {
                        "schema_version": "iss-candles.v1",
                        "secid": secid,
                        "interval": 24,
                        "rows": [
                            {
                                "begin": f"{day.isoformat()} 00:00:00",
                                "open": 10 + offset + i * 0.01 + (i % 8) * 0.003 - 0.002,
                                "close": 10 + offset + i * 0.01 + (i % 8) * 0.003,
                                "volume": 500,
                                "price_proxy_kind": "candle_close",
                            }
                            for i, day in enumerate(days)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            cache_path = inputs.history_cache(pair_id) / f"{secid}-{start}-{end}-0.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            history_rows = [
                [
                    "RFUD",
                    day.isoformat(),
                    secid,
                    10 + offset + i * 0.01,
                    10 + offset + (i - 1) * 0.01 if i > 0 else 10 + offset,
                    0.015 if secid == near else None,
                    1000,
                    10000.0,
                    500,
                    10 + offset + i * 0.01,
                    1,
                    None,
                ]
                for i, day in enumerate(days)
            ]
            cached_payload = {
                "history": {"columns": list(HISTORY_FIELDS), "data": history_rows},
                "history.cursor": {
                    "columns": ["INDEX", "TOTAL", "PAGESIZE"],
                    "data": [[0, len(history_rows), 100]],
                },
            }
            cache_path.write_text(
                json.dumps(
                    {
                        "url": history_url(secid, start, end, 0, HISTORY_FIELDS),
                        "payload": cached_payload,
                        "cached_at": datetime.now(UTC).isoformat(),
                        "raw_response_sha256": ("cd" if secid == near else "ef") * 32,
                    }
                ),
                encoding="utf-8",
            )
    return days


def test_parameters_validation_and_runtime_guards() -> None:
    assert _parameters({}, "mean_reversion") == {"lookback": 20, "entry_z": 2.0, "exit_z": 0.5}
    assert _parameters({"parameters": {"lookback": 10}}, "funding_only") == {"lookback": 10}
    for bad_params in (cast(Any, ["not-a-dict"]), cast(Any, "bad"), {"unknown": 1}):
        with pytest.raises(ScreenerError, match="неизвестный параметр"):
            _parameters({"parameters": bad_params}, "mean_reversion")
    for bad_lookback in (True, 4, 81, 10.5):
        with pytest.raises(ScreenerError, match="lookback"):
            _parameters({"parameters": {"lookback": bad_lookback}}, "mean_reversion")
    for bad_num in (False, float("nan"), float("inf"), "2.0"):
        with pytest.raises(ScreenerError, match="конечным числом"):
            _parameters({"parameters": {"entry_z": bad_num}}, "mean_reversion")
    for entry_z, exit_z in ((0.4, 0.1), (5.1, 0.5), (2.0, -0.1), (1.5, 1.5)):
        with pytest.raises(ScreenerError, match="entry должен быть"):
            _parameters(
                {"parameters": {"entry_z": entry_z, "exit_z": exit_z}},
                "mean_reversion",
            )


def test_execution_settings_validation() -> None:
    assert execution_settings({}) == {"slippage_ticks": 1.0, "quantity": 1}
    assert execution_settings({"slippage_ticks": 0, "quantity": 3}) == {
        "slippage_ticks": 0.0,
        "quantity": 3,
    }
    for bad_slip in (True, -0.1, 10.1, "1"):
        with pytest.raises(ScreenerError, match="slippage"):
            execution_settings({"slippage_ticks": bad_slip})
    for bad_qty in (False, 0, MAX_QUANTITY + 1, 1.5):
        with pytest.raises(ScreenerError, match="контрактов"):
            execution_settings({"quantity": bad_qty})


def test_build_result_rejects_invalid_selection_and_intraday_carry(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    inputs = ScreenerInputs(tmp_path)
    with pytest.raises(ScreenerError, match="выбор должен быть объектом"):
        build_result(inputs, cast(Any, ["not-a-mapping"]))
    with pytest.raises(ScreenerError, match="неизвестная стратегия"):
        build_result(inputs, {"pair": "CNYRUBF/CRZ6", "strategy": "unknown"})
    save_settings(tmp_path, {"timeframe": "1h"})
    with pytest.raises(ScreenerError, match="carry требует дневных"):
        build_result(inputs, {"pair": "CNYRUBF/CRZ6", "strategy": "funding_only"})


def test_build_result_branches_preserve_deterministic_run_ids(tmp_path: Path) -> None:
    days = _write_fixture(tmp_path)
    inputs = ScreenerInputs(tmp_path)

    mr = build_result(
        inputs,
        {"pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion", "slippage_ticks": 1.0},
    )
    assert (
        mr["run_id"] == "7a8a67724b90280f"
    )  # re-recorded for #272 (effective_validation_percent in payload)
    assert mr["metrics_scope"] == "full_history"

    mr_through = build_result(
        inputs,
        {
            "pair": "GLDRUBF/GLZ6",
            "strategy": "mean_reversion",
            "parameters": {"lookback": 15, "entry_z": 1.5, "exit_z": 0.4},
            "quantity": 2,
        },
        through=days[30].isoformat(),
    )
    assert mr_through["run_id"] == "64ba32b9f983f9fc"
    assert "evaluation" not in mr_through

    carry_cny = build_result(
        inputs,
        {
            "pair": "CNYRUBF/CRZ6",
            "strategy": "funding_only",
            "parameters": {"lookback": 10},
            "slippage_ticks": 0.5,
        },
        validation_share=20.0,
    )
    assert carry_cny["run_id"] == "6e6d2613a5951dea"  # re-recorded for #272
    assert all("Non-CNY" not in note for note in carry_cny["notes"])

    carry_gld = build_result(
        inputs,
        {"pair": "GLDRUBF/GLZ6", "strategy": "funding_only", "parameters": {"lookback": 10}},
    )
    assert carry_gld["run_id"] == "a8d155d159e546cd"  # re-recorded for #272
    assert any("Non-CNY funding-rule caps" in note for note in carry_gld["notes"])


def test_pair_screener_uses_bounded_caches_and_curated_exports():
    assert isinstance(inputs_module._CANDLE_SUMMARY_CACHE, HashedJSONCache)
    assert isinstance(normalization_module._NORMALIZED_BY_TS_CACHE, HashedJSONCache)
    assert not {"annotations", "Any", "dataclass", "math"} & set(inputs_module.__all__)
    assert not {"annotations", "Any", "bisect_left", "partial"} & set(normalization_module.__all__)
    assert not hasattr(ScreenerInputs, "screen_history")
    assert not hasattr(ScreenerInputs, "search_history")


def test_build_result_honors_initial_capital_across_branches_and_stress(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    save_settings(tmp_path, {"initial_capital_rub": 100000.0})
    inputs = ScreenerInputs(tmp_path)

    # quantity=4 fails VTB stress at 30k RUB, but passes at 100k RUB.
    mr = build_result(
        inputs,
        {"pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion", "quantity": 4},
    )
    assert mr["daily"][0]["V"] == pytest.approx(100000.0)
    assert mr["daily"][0]["B"] == pytest.approx(100000.0)
    assert mr["metrics"]["lqdt_return_pct"] == pytest.approx(
        round((mr["daily"][-1]["B"] / 100000.0 - 1) * 100, 3)
    )
    assert mr["metrics"]["lqdt_return_pct"] > 0

    carry = build_result(
        inputs,
        {
            "pair": "CNYRUBF/CRZ6",
            "strategy": "funding_only",
            "parameters": {"lookback": 10},
            "quantity": 4,
        },
    )
    assert carry["daily"][0]["B"] == pytest.approx(100000.0)
    assert carry["metrics"]["lqdt_return_pct"] == pytest.approx(
        round((carry["daily"][-1]["B"] / 100000.0 - 1) * 100, 3)
    )
    assert carry["metrics"]["lqdt_return_pct"] > 0


def test_build_result_through_ignores_post_cutoff_thin_volume_and_funding_gaps(
    tmp_path: Path,
) -> None:
    days = _write_fixture(tmp_path)
    cutoff = days[30].isoformat()
    late_day = days[-1].isoformat()

    # Inject post-through thin volume (< MIN_DAILY_VOLUME) on CNYRUBF and GLDRUBF candles.
    for secid in ("CNYRUBF", "GLDRUBF"):
        candle_path = tmp_path / "data/w1/candles" / f"{secid}_1d.json"
        doc = json.loads(candle_path.read_text(encoding="utf-8"))
        for row in doc["rows"]:
            if str(row["begin"])[:10] == late_day:
                row["volume"] = 5
        candle_path.write_text(json.dumps(doc), encoding="utf-8")

    # Also drop post-through SWAPRATE on CNYRUBF across 4 trailing sessions (> MAX_GAP).
    start, end = HISTORY_WINDOWS["CNYRUBF/CRZ6"]
    hist_path = tmp_path / "data/carry-history-cache" / f"CNYRUBF-{start}-{end}-0.json"
    hist_doc = json.loads(hist_path.read_text(encoding="utf-8"))
    block = hist_doc["payload"]["history"]
    date_idx = block["columns"].index("TRADEDATE")
    swap_idx = block["columns"].index("SWAPRATE")
    trailing = {d.isoformat() for d in days[-4:]}
    for row in block["data"]:
        if row[date_idx] in trailing:
            row[swap_idx] = None
    hist_path.write_text(json.dumps(hist_doc), encoding="utf-8")

    inputs = ScreenerInputs(tmp_path)
    # Full-span run fails closed because of post-cutoff thin volume / funding gap:
    with pytest.raises(ScreenerError):
        build_result(inputs, {"pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"})
    # Causal slice up to cutoff succeeds without leaking post-cutoff issues:
    res = build_result(
        inputs,
        {
            "pair": "CNYRUBF/CRZ6",
            "strategy": "mean_reversion",
            "parameters": {"lookback": 10, "entry_z": 1.5, "exit_z": 0.5},
        },
        through=cutoff,
    )
    assert res["daily"][-1]["date"] <= cutoff


def test_pair_margin_metrics_rejects_bool_nan_inf_and_non_positive_values() -> None:
    assert _pair_margin_metrics({"initial_margin": True}, {"initial_margin": 1000.0}, 30000.0) == (
        None,
        None,
        None,
    )
    assert _pair_margin_metrics(
        {"initial_margin": float("nan")}, {"initial_margin": 1000.0}, 30000.0
    ) == (None, None, None)
    assert _pair_margin_metrics(
        {"initial_margin": float("inf")}, {"initial_margin": 1000.0}, 30000.0
    ) == (None, None, None)
    assert _pair_margin_metrics(
        {"initial_margin": -5000.0}, {"initial_margin": 1000.0}, 30000.0
    ) == (None, None, None)
    assert _pair_margin_metrics({"initial_margin": 1000.0}, {"initial_margin": 1000.0}, 0.0) == (
        None,
        None,
        None,
    )
    assert _pair_margin_metrics(
        {"initial_margin": 1000.0}, {"initial_margin": 2000.0}, 30000.0
    ) == (10.0, 3000.0, True)
