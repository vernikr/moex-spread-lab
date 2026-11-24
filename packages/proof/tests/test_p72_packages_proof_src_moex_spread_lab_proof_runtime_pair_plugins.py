"""Regression coverage for strict typing of ``proof_runtime.pair_plugins`` (Issue #88).

These tests pin the behavior-preserving strict-typing refactor of
``packages/proof/src/moex_spread_lab/proof/pair_plugins.py``:

* the public ``run_id`` alias replaces the private ``_run_id`` cross-module import;
* the module-level ``cast`` bindings for the not-yet-strict accounting callables
  resolve to the exact source functions (no behavior change);
* the ``PairPluginRoute`` replay callable still rejects malformed requests at the
  boundary (the ``cast``-to-``dict`` boundary refactor preserves the guard);
* ``_enriched_trade`` still rebuilds fill blocks and carries ``margin_rub`` /
  ``sizing`` through unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from moex_spread_lab.core import w0
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof import pair_plugin_accounting as accounting
from moex_spread_lab.proof import pair_plugins
from moex_spread_lab.proof.pair_plugins import (
    PairPluginRoute,
    _enriched_trade,
)

PAIR_ID = "GLDRUBF/GLZ6"
STRATEGY_ID = "p72_stub_plugin"
DAYS = ["2026-01-01", "2026-01-02"]


class _StubPlugin:
    strategy_id = STRATEGY_ID
    requires_daily_bars = True


class _StubService:
    """Minimal service exposing only the surface ``PairPluginRoute.prepare`` touches."""

    def __init__(self, series: dict[str, Any], benchmark_path: Path, native):
        self.screener_inputs = SimpleNamespace(benchmark=benchmark_path)
        self._series = series
        self._native = native

    def _price_inputs(self, inputs, pair, funding, validation_percent, timeframe="1d"):
        return self._series, [], None, {"stub": True}

    def prepare(self, selection, *, calendar=None, settings=None):
        return self._native

    def _prepare_native(self, selection, *, calendar=None, settings=None):
        return self._native


def _series() -> dict[str, Any]:
    return {
        "rows": [
            {"date": day, "benchmark_price": 1.0, "near_close": 100.0 + i, "far_close": 99.0}
            for i, day in enumerate(DAYS)
        ],
        "contracts": {
            "GLDRUBF": {"tick_size": 1.0, "tick_value": 1.0},
            "GLZ6": {"tick_size": 1.0, "tick_value": 10.0},
        },
    }


def _benchmark_path(tmp_path: Path) -> Path:
    path = tmp_path / "benchmark.json"
    path.write_text(
        json.dumps({"rows": [{"date": day, "price": 1.0} for day in DAYS]}),
        encoding="utf-8",
    )
    return path


def _route(tmp_path: Path) -> PairPluginRoute:
    series = _series()

    def native(request, through=None, **_kwargs):
        return {
            "series": [
                {"date": row["date"], "signal_z": 9.9, "chart_col": "native"}
                for row in series["rows"]
            ],
            "notes": ["native note"],
            "provenance": {"native": "meta"},
            "selection": {"strategy": "mean_reversion"},
        }

    service = _StubService(series, _benchmark_path(tmp_path), native)
    selection = {
        "universe": "pair_screen",
        "pair": PAIR_ID,
        "strategy": STRATEGY_ID,
        "sizing": "max_affordable",
        "parameters": {},
    }
    return PairPluginRoute(service, selection, ResearchSettings(), _StubPlugin())


def test_run_id_public_alias_matches_private_and_module() -> None:
    # The strict refactor swapped the private ``_run_id`` import for the public
    # ``run_id`` alias; both must resolve to the exact same callable.
    assert pair_plugins.run_id is w0.run_id
    assert pair_plugins.run_id is w0._run_id


def test_accounting_callables_are_bound_to_source_module() -> None:
    # The module-level ``cast`` bindings must be the source functions themselves
    # (``cast`` is a runtime no-op), so runtime behavior is unchanged.
    assert pair_plugins._prepare_daily_pair_unit is accounting.prepare_daily_pair_unit
    assert pair_plugins._size_daily_pair_plugin is accounting.size_daily_pair_plugin


def test_route_replay_rejects_non_dict_request(tmp_path: Path) -> None:
    replay = _route(tmp_path).prepare()
    with pytest.raises(W0Error):
        replay("not-a-dict")  # type: ignore[arg-type]


def test_route_replay_rejects_mismatched_contract(tmp_path: Path) -> None:
    replay = _route(tmp_path).prepare()
    base = {
        "universe": "pair_screen",
        "pair": PAIR_ID,
        "strategy": STRATEGY_ID,
        "sizing": "max_affordable",
        "parameters": {},
    }
    with pytest.raises(W0Error):
        replay({**base, "strategy": "other_strategy"})
    with pytest.raises(W0Error):
        replay({**base, "universe": "calendar_native"})
    with pytest.raises(W0Error):
        replay({**base, "pair": "IMOEXF/MMZ6"})
    # Automatic sizing must not carry an explicit quantity.
    with pytest.raises(W0Error):
        replay({**base, "quantity": 2})
    # Sizing mode must match the prepared window.
    with pytest.raises(W0Error):
        replay({**base, "sizing": "fixed"})


def _trade_row() -> dict[str, Any]:
    return {
        "entry": {
            "date": "2026-01-01",
            "spread_fill": 1.0,
            "near_price": 100.0,
            "far_price": 99.0,
            "costs": {"broker": 0.1},
            "margin_rub": 5.0,
        },
        "exit": {
            "date": "2026-01-02",
            "spread_fill": 2.0,
            "near_price": 101.0,
            "far_price": 99.0,
            "costs": None,
            "margin_rub": 5.0,
        },
        "number": 1,
        "side": "long_spread",
        "quantity": 2,
        "signal": {"date": "2026-01-01", "kind": "z", "value": 2.5},
        "exit_reason": "mean_reversion_exit",
        "margin_rub": 5.0,
        "gross_rub": 3.0,
        "costs_rub": 0.1,
        "net_rub": 2.9,
        "open_at_end": False,
        "funding_rub": 0.2,
    }


def _indexed() -> dict[str, dict[str, Any]]:
    return {
        day: {
            "date": day,
            "benchmark_mark_date": day,
            "near_volume": 300,
            "far_volume": 400,
            "spread_volume": 300,
        }
        for day in DAYS
    }


_BENCHMARK: tuple[list[str], dict[str, float]] = (DAYS, dict.fromkeys(DAYS, 1.0))


def test_enriched_trade_rebuilds_fill_blocks_and_margin() -> None:
    trade = _enriched_trade(_trade_row(), _indexed(), far_mult=10.0, benchmark=_BENCHMARK)
    entry, exit_ = trade["entry"], trade["exit"]
    # Fill blocks are rebuilt against the enriched series indices.
    assert entry["date"] == "2026-01-01"
    assert exit_["date"] == "2026-01-02"
    assert entry["spread_fill"] == 1.0
    assert exit_["spread_fill"] == 2.0
    # ``far_value`` is recomputed from ``far_price * far_mult``.
    assert entry["far_price"] == 99.0
    # ``margin_rub`` is carried from the raw event onto the rebuilt block.
    assert entry["margin_rub"] == 5.0
    assert exit_["margin_rub"] == 5.0
    # Costs are normalized into the standard cost envelope.
    assert entry["costs"]["broker"] == 0.1
    assert exit_["costs"]["broker"] == 0.0
    # The trade-level scalars and funding pass through unchanged.
    assert trade["number"] == 1
    assert trade["side"] == "long_spread"
    assert trade["quantity"] == 2
    assert trade["funding_rub"] == 0.2
    assert trade["open_at_end"] is False


def test_enriched_trade_passes_through_sizing() -> None:
    trade = _trade_row()
    trade["sizing"] = {"quantity": 2, "policy": "max_affordable"}
    result = _enriched_trade(trade, _indexed(), far_mult=1.0, benchmark=_BENCHMARK)
    assert result["sizing"] == {"quantity": 2, "policy": "max_affordable"}


def test_enriched_trade_omits_sizing_when_absent() -> None:
    result = _enriched_trade(_trade_row(), _indexed(), far_mult=1.0, benchmark=_BENCHMARK)
    assert "sizing" not in result
