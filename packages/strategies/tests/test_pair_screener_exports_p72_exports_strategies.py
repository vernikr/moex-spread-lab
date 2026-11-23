"""Wildcard and hook-contract regressions for P7.2 pair-screener exports (#139).

Pre-leaf wildcard sets were captured from each module's actual immediate
pre-leaf revision (not the historical ``545cf0c`` snapshot): ``inputs.py`` at
``128a79794bfe6827a2f0acd5dcb62aef658eb505`` and ``normalization.py`` at
``ad59ea6e3894b4a36729711ffe51fd4c10fdfc44``; both equal the ``545cf0c`` sets.

Both modules preserve their measured pre-leaf wildcard names alongside the
private facade hooks explicitly exported in the intervening P7.2 leaves. This
is an additive superset, not exact pre-leaf wildcard parity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from moex_spread_lab.core import timeframes, universe, w0
from moex_spread_lab.data.lake import data_lake
from moex_spread_lab.data.lake.data_lake import BarSeries
from moex_spread_lab.strategies.funding_carry import carry, proof_funding
from moex_spread_lab.strategies.pair_mean_reversion import analytics
from moex_spread_lab.strategies.pair_screener import inputs, normalization, screener
from moex_spread_lab.strategies.pair_screener import pair_screener as facade

INPUTS_PRE_LEAF_WILDCARD = frozenset(
    {
        "Any",
        "BarSeries",
        "Callable",
        "DEFAULT_TIMEFRAME",
        "HISTORY_WINDOWS",
        "Mapping",
        "MarketDataStore",
        "PAIRS",
        "Path",
        "ProofFunding",
        "ScreenerError",
        "ScreenerInputs",
        "W0Error",
        "annotations",
        "cached_property",
        "dataclass",
        "fetch_history",
        "get_timeframe",
        "math",
        "pair_price_inputs",
    }
)
INPUTS_FACADE_HOOKS = frozenset(
    {
        "_CANDLE_SUMMARY_CACHE",
        "_candle_summary",
        "_history_source",
        "_leg_volumes",
        "_make_proof_funding",
        "_offline_only",
        "_pair",
        "_pair_history_span",
        "_positive_leg_volume_dates",
        "_session_volumes",
    }
)
NORMALIZATION_PRE_LEAF_WILDCARD = frozenset(
    {
        "Any",
        "BarSeries",
        "Collection",
        "DataLakeError",
        "LegacyJsonBarStore",
        "Path",
        "ScreenError",
        "W0Error",
        "annotations",
        "bisect_left",
        "get_timeframe",
        "load_contracts",
        "partial",
        "read_hashed_json",
        "timeframe_for_interval",
    }
)
NORMALIZATION_FACADE_HOOKS = frozenset(
    {"_bar_series", "_normalized_series", "_read_json", "_round"}
)


class _FakeSeries:
    def __init__(self, cache_key: tuple[Any, ...], rows: tuple[dict[str, Any], ...]) -> None:
        self.cache_key = cache_key
        self.rows = rows


def _star_namespace(dotted: str) -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec(f"from {dotted} import *", namespace)
    return namespace


def _star_names(dotted: str) -> set[str]:
    return {name for name in _star_namespace(dotted) if name != "__builtins__"}


def test_inputs_wildcard_restores_the_pre_leaf_surface() -> None:
    expected = INPUTS_PRE_LEAF_WILDCARD | INPUTS_FACADE_HOOKS
    assert _star_names("moex_spread_lab.strategies.pair_screener.inputs") == expected
    assert set(inputs.__all__) == expected
    # No leaf-only helper leaks into the wildcard surface.
    assert "cast" not in inputs.__all__
    assert "_universe" not in inputs.__all__
    for name in expected:
        assert hasattr(inputs, name), name


def test_normalization_restores_pre_leaf_surface_and_declared_facade() -> None:
    expected = NORMALIZATION_PRE_LEAF_WILDCARD | NORMALIZATION_FACADE_HOOKS
    assert _star_names("moex_spread_lab.strategies.pair_screener.normalization") == expected
    assert set(normalization.__all__) == expected
    for name in expected:
        assert hasattr(normalization, name), name
    assert "_path_source" not in normalization.__all__
    assert normalization._path_source is not None


def test_registry_aliases_keep_core_object_identity() -> None:
    namespace = _star_namespace("moex_spread_lab.strategies.pair_screener.inputs")
    assert inputs.HISTORY_WINDOWS is universe.HISTORY_WINDOWS
    assert inputs.PAIRS is universe.PAIRS
    assert namespace["HISTORY_WINDOWS"] is universe.HISTORY_WINDOWS
    assert namespace["PAIRS"] is universe.PAIRS


def test_pair_screener_facade_keeps_inputs_hook_identity() -> None:
    for name in sorted(INPUTS_FACADE_HOOKS):
        assert getattr(facade, name) is getattr(inputs, name), name
    for name in ("ScreenerError", "ScreenerInputs", "PAIRS", "HISTORY_WINDOWS"):
        assert getattr(facade, name) is getattr(inputs, name), name
    assert facade.fetch_history is inputs.fetch_history


def test_screener_facade_keeps_normalization_hook_identity() -> None:
    for name in sorted(NORMALIZATION_FACADE_HOOKS):
        assert getattr(screener, name) is getattr(normalization, name), name


def test_inputs_wildcard_names_keep_origin_identity() -> None:
    namespace = _star_namespace("moex_spread_lab.strategies.pair_screener.inputs")
    origins = {
        "DEFAULT_TIMEFRAME": timeframes.DEFAULT_TIMEFRAME,
        "HISTORY_WINDOWS": universe.HISTORY_WINDOWS,
        "PAIRS": universe.PAIRS,
        "BarSeries": data_lake.BarSeries,
        "MarketDataStore": data_lake.MarketDataStore,
        "ProofFunding": proof_funding.ProofFunding,
        "W0Error": w0.W0Error,
        "fetch_history": carry.fetch_history,
        "get_timeframe": timeframes.get_timeframe,
    }
    for name, origin in origins.items():
        assert getattr(inputs, name) is origin, name
        assert namespace[name] is origin, name


def test_normalization_named_exports_keep_origin_identity() -> None:
    origins = {
        "BarSeries": data_lake.BarSeries,
        "DataLakeError": data_lake.DataLakeError,
        "LegacyJsonBarStore": data_lake.LegacyJsonBarStore,
        "W0Error": w0.W0Error,
        "get_timeframe": timeframes.get_timeframe,
        "load_contracts": analytics.load_contracts,
        "read_hashed_json": w0.read_hashed_json,
        "timeframe_for_interval": timeframes.timeframe_for_interval,
    }
    for name, origin in origins.items():
        assert getattr(normalization, name) is origin, name


def test_shared_caches_are_module_level_and_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    assert facade._CANDLE_SUMMARY_CACHE is inputs._CANDLE_SUMMARY_CACHE

    series = _FakeSeries(("p72-139-inputs",), ({"begin": "2026-01-01 00:00:00", "volume": 10.0},))
    inputs_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    monkeypatch.setattr(inputs, "_CANDLE_SUMMARY_CACHE", inputs_cache)
    summary = inputs._candle_summary(cast(BarSeries, series))
    assert inputs_cache[series.cache_key] is summary
    assert summary["volumes"]["2026-01-01"] == 10.0

    norm_series = _FakeSeries(
        ("p72-139-normalization",),
        ({"begin": "2026-01-01 00:00:00", "close": 1.5},),
    )
    norm_cache: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}
    monkeypatch.setattr(normalization, "_NORMALIZED_BY_TS_CACHE", norm_cache)
    rows = normalization._rows_by_ts(cast(BarSeries, norm_series))
    assert norm_cache[norm_series.cache_key] is rows
    assert set(rows) == {"2026-01-01T00:00:00"}


def test_candle_summary_hook_is_late_bound(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[Any] = []

    def patched(series: Any) -> dict[str, Any]:
        calls.append(series)
        raise RuntimeError("patched _candle_summary")

    monkeypatch.setattr(inputs, "_candle_summary", patched)
    monkeypatch.setattr(inputs.ScreenerInputs, "bars", lambda self, *args, **kwargs: object())
    with pytest.raises(RuntimeError, match="patched _candle_summary"):
        inputs._session_volumes(inputs.ScreenerInputs(tmp_path), inputs.PAIRS[0])
    assert len(calls) == 1


def test_normalized_series_hook_is_late_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def patched(*args: Any, **kwargs: Any) -> str:
        return "patched-series"

    monkeypatch.setattr(inputs, "_normalized_series", patched)
    monkeypatch.setattr(inputs.ScreenerInputs, "bars", lambda self, *args, **kwargs: object())
    pair = {"pair_id": "P72/139", "near": "NEAR", "far": "FAR", "family": "other"}
    series, book = inputs.pair_price_inputs(inputs.ScreenerInputs(tmp_path), pair, None)
    assert series == "patched-series"
    assert book is None


def test_history_source_hook_is_late_bound(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def patched(inp: Any, pair_id: str, secid: str, **kwargs: Any) -> dict[str, Any]:
        return {"rows": [], "patched": (pair_id, secid)}

    monkeypatch.setattr(inputs, "_history_source", patched)
    funding = inputs._make_proof_funding(
        inputs.ScreenerInputs(tmp_path), fetch_fn=lambda *args, **kwargs: {"rows": []}
    )
    pair_id = inputs.PAIRS[0]["pair_id"]
    source = funding.source(pair_id, "NEAR")
    assert source == {"rows": [], "patched": (pair_id, "NEAR")}


def test_positive_volume_dates_hook_is_late_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def patched(*args: Any, **kwargs: Any) -> set[str]:
        return {"2026-01-01"}

    monkeypatch.setattr(inputs, "_positive_leg_volume_dates", patched)
    funding = inputs._make_proof_funding(
        inputs.ScreenerInputs(tmp_path), fetch_fn=lambda *args, **kwargs: {"rows": []}
    )
    assert funding._positive_volume_dates_fn is patched


def test_bar_series_hook_is_late_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def patched(source: Any) -> BarSeries:
        calls.append(source)
        raise normalization.ScreenError("patched _bar_series")

    monkeypatch.setattr(normalization, "_bar_series", patched)
    with pytest.raises(normalization.ScreenError, match="patched _bar_series"):
        normalization._normalized_series(
            Path("near.json"),
            Path("far.json"),
            Path("benchmark.json"),
            Path("contracts.json"),
            near="NEAR",
            far="FAR",
        )
    assert calls == [Path("near.json")]


def test_read_json_hook_is_late_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    series = BarSeries(
        schema_version="iss-candles.v1",
        secid="NEAR",
        interval=24,
        timeframe="1d",
        rows=(),
        source_path="near.json",
        source_sha256="sha",
        backend="json",
        cache_key=("p72-139-read-json",),
        metadata={},
    )
    monkeypatch.setattr(normalization, "_bar_series", lambda source: series)

    def patched(path: Any) -> tuple[dict[str, Any], str]:
        raise normalization.ScreenError(f"patched _read_json: {path}")

    monkeypatch.setattr(normalization, "_read_json", patched)
    with pytest.raises(normalization.ScreenError, match="patched _read_json"):
        normalization._normalized_series(
            Path("near.json"),
            Path("far.json"),
            Path("benchmark.json"),
            Path("contracts.json"),
            near="NEAR",
            far="FAR",
        )


def test_offline_guard_and_pair_lookup_stay_intact() -> None:
    with pytest.raises(OSError, match="does not download"):
        inputs._offline_only("https://iss.moex.com")
    pair = inputs._pair(inputs.PAIRS[0]["pair_id"])
    assert set(pair) >= {"pair_id", "near", "far", "family"}
    assert inputs.pair_price_inputs.__name__ == "pair_price_inputs"
