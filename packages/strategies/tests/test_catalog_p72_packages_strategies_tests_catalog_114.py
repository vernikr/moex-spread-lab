"""Regression coverage for the strict typing of ``pair_screener.catalog`` (Issue #114).

The change under test is typing-only: typed aliases for the core research registry, a narrow
``FundingStatuses`` protocol instead of ``Any``, named instrument/entry aliases, and clarified
intermediate annotations. These tests pin the runtime behaviour those annotations describe,
plus the payload shape and facade identity that must not move.
"""

from __future__ import annotations

import importlib
import json
import math
from pathlib import Path
from typing import Any

import pytest

from catalog_fixtures_p72_packages_strategies_tests_catalog_114 import (
    CAPITAL,
    DAYS,
    ENTRY_KEYS,
    FAR,
    NEAR,
    PAIR,
    PAIR_ID,
    StubFunding,
    build_root,
    write_json,
)
from moex_spread_lab.core import universe
from moex_spread_lab.core.timeframes import TimeframeSpec, get_timeframe
from moex_spread_lab.strategies.pair_screener import pair_screener as facade
from moex_spread_lab.strategies.pair_screener.catalog import (
    _catalog_pair_entry,
    _load_instruments,
    _pair_margin_metrics,
    _resolve_proof_availability,
    catalog,
)
from moex_spread_lab.strategies.pair_screener.inputs import ScreenerInputs

catalog_module: Any = importlib.import_module("moex_spread_lab.strategies.pair_screener.catalog")
# A spec without any optional proof adapter; the helpers only read the capability flags.
NO_PROOF_SPEC = TimeframeSpec("5m", "5 минут", 5, "5m", True, False, False, False)


def _entry(root: Path, funding: Any = None, **kwargs: Any) -> dict[str, Any]:
    inputs = ScreenerInputs(root)
    spec = get_timeframe("1d")
    entry: dict[str, Any] = _catalog_pair_entry(
        inputs,
        PAIR,
        spec,
        "1d",
        funding or StubFunding(),
        _load_instruments(inputs),
        CAPITAL,
        **kwargs,
    )
    return entry


def test_registry_aliases_keep_core_identity() -> None:
    assert catalog_module.HISTORY_WINDOWS is universe.HISTORY_WINDOWS
    assert catalog_module.PAIRS is universe.PAIRS
    assert catalog_module.STRATEGIES is universe.STRATEGIES
    assert catalog_module.INITIAL_CAPITAL == universe.INITIAL_CAPITAL
    assert {pair["pair_id"] for pair in catalog_module.PAIRS} <= set(catalog_module.HISTORY_WINDOWS)


def test_facade_still_reexports_the_same_catalog() -> None:
    assert facade.catalog is catalog


def test_load_instruments_layers_historical_terms(tmp_path: Path) -> None:
    build_root(tmp_path)
    inputs = ScreenerInputs(tmp_path)
    current = _load_instruments(inputs)
    assert set(current) == {NEAR, FAR}
    assert current[NEAR]["initial_margin"] == 1000

    write_json(
        tmp_path / "data/w1/historical-contracts.json",
        {
            "instruments": [
                {"secid": NEAR, "initial_margin": 1500, "historical": True},
                {"secid": "GDZ5", "initial_margin": 700},
                {"secid": None},
                "not-a-row",
            ]
        },
    )
    layered = _load_instruments(inputs)
    assert set(layered) == {NEAR, FAR, "GDZ5"}
    assert layered[NEAR]["initial_margin"] == 1500
    assert layered[NEAR]["historical"] is True
    # A historical row replaces the current one wholesale; it does not merge keys.
    assert "expiry" not in layered[NEAR]


@pytest.mark.parametrize(
    "payload",
    [None, "{not json", {"instruments": "nope"}, ["list", "payload"], {"instruments": []}],
)
def test_load_instruments_tolerates_missing_and_malformed_documents(
    tmp_path: Path, payload: Any
) -> None:
    inputs = ScreenerInputs(tmp_path)
    assert _load_instruments(inputs) == {}
    if payload is None:
        return
    document = tmp_path / "data/w1/current-intake.json"
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text(payload if isinstance(payload, str) else json.dumps(payload), "utf-8")
    assert _load_instruments(inputs) == {}


def test_pair_margin_metrics_require_positive_finite_margins() -> None:
    both = {"initial_margin": 2000}
    assert _pair_margin_metrics({"initial_margin": 1000}, both, CAPITAL) == (3.0, 3000.0, True)
    # Percentage still reports when the strict cap cannot be evaluated.
    assert _pair_margin_metrics({"initial_margin": 0}, both, CAPITAL) == (2.0, None, None)
    assert _pair_margin_metrics({}, both, CAPITAL) == (None, None, None)
    # ``bool`` passes the loose percentage check (it is an int) but never the cap check.
    assert _pair_margin_metrics({"initial_margin": True}, both, CAPITAL) == (2.0, None, None)
    # An infinite margin renders as ``inf`` for the reader but yields no cap verdict.
    assert _pair_margin_metrics({"initial_margin": math.inf}, both, CAPITAL) == (
        math.inf,
        None,
        None,
    )
    assert _pair_margin_metrics({"initial_margin": "1000"}, both, CAPITAL) == (None, None, None)


def test_margin_cap_uses_stress_and_uncertainty_buffers() -> None:
    from moex_spread_lab.accounting.account_risk import (
        DEFAULT_STRESS_VM_BUFFER,
        DEFAULT_UNCERTAINTY_BUFFER,
    )

    legs = {"initial_margin": 10_000.0}
    pair_margin = 20_000.0
    capital = 3 * pair_margin + DEFAULT_STRESS_VM_BUFFER + DEFAULT_UNCERTAINTY_BUFFER
    assert _pair_margin_metrics(legs, legs, capital) == (
        round(pair_margin / capital * 100, 2),
        pair_margin,
        True,
    )
    assert _pair_margin_metrics(legs, legs, capital - 1)[2] is False


def test_resolve_proof_availability_delegates_when_funding_is_supported(
    tmp_path: Path,
) -> None:
    build_root(tmp_path)
    funding = StubFunding(available=True)
    days = {"2026-01-01", "2026-01-02"}
    price_files = (tmp_path / "price.json",)
    funding_files = (tmp_path / "funding.json",)
    proof_available, reasons = _resolve_proof_availability(
        ScreenerInputs(tmp_path),
        PAIR,
        get_timeframe("1d"),
        funding,
        days,
        5,
        (price_files, funding_files),
    )
    assert proof_available == {"mean_reversion": True, "funding_only": True}
    assert reasons == {}
    assert funding.calls == [(PAIR, days, price_files, funding_files)]


def test_resolve_proof_availability_skips_funding_days_for_non_perpetual(
    tmp_path: Path,
) -> None:
    build_root(tmp_path)
    funding = StubFunding()
    quarter_pair = {**PAIR, "family": "quarterly_quarterly"}
    _resolve_proof_availability(
        ScreenerInputs(tmp_path),
        quarter_pair,
        get_timeframe("1d"),
        funding,
        {"2026-01-01"},
        5,
        ((), ()),
    )
    assert funding.calls[0][1] == set()


def test_resolve_proof_availability_reports_unsupported_interval(tmp_path: Path) -> None:
    build_root(tmp_path)
    proof_available, reasons = _resolve_proof_availability(
        ScreenerInputs(tmp_path), PAIR, NO_PROOF_SPEC, StubFunding(), set(), 5, ((), ())
    )
    assert proof_available == {"mean_reversion": False, "funding_only": False}
    assert reasons["mean_reversion"] == (
        f"{NO_PROOF_SPEC.label}: ценовой pair-proof не реализован для этого интервала."
    )
    assert reasons["funding_only"] == (
        f"{NO_PROOF_SPEC.label}: carry требует дневных расчётных ставок."
    )


def test_resolve_proof_availability_reports_missing_local_price_file(tmp_path: Path) -> None:
    build_root(tmp_path)
    hourly = get_timeframe("1h")
    assert hourly.supports_funding_strategy is False
    missing = tmp_path / "data/w1/candles-screen" / f"{NEAR}_1h.json"
    proof_available, reasons = _resolve_proof_availability(
        ScreenerInputs(tmp_path), PAIR, hourly, StubFunding(), set(), 5, ((missing,), ())
    )
    assert proof_available == {"mean_reversion": False, "funding_only": False}
    assert reasons["mean_reversion"] == f"Нет локальных данных: {missing.name}."
    assert reasons["funding_only"] == f"{hourly.label}: carry требует дневных расчётных ставок."


def test_catalog_entry_payload_shape_and_status(tmp_path: Path) -> None:
    build_root(tmp_path)
    entry = _entry(tmp_path)
    assert set(entry) == ENTRY_KEYS
    assert entry["id"] == PAIR_ID
    assert entry["family"] == "perpetual_quarterly_same_asset"
    assert entry["observations"] == len(DAYS)
    assert entry["from"] == DAYS[0] and entry["to"] == DAYS[-1]
    assert entry["expiry"] == "2026-12-17"
    assert entry["max_pair_margin_rub"] == 2000.0
    assert entry["current_margin_pct"] == 2.0
    assert entry["min_leg_daily_volume"] == 1000.0
    assert entry["status"] == "local_inputs_available"


def test_catalog_entry_marks_thin_leg(tmp_path: Path) -> None:
    build_root(tmp_path, far_volume=10)
    entry = _entry(tmp_path)
    assert entry["status"] == "excluded_thin_leg"
    assert entry["tradable_every_day"] is False
    assert entry["strategies"] == {"mean_reversion": False, "funding_only": False}


def test_catalog_entry_without_instruments_reports_null_terms(tmp_path: Path) -> None:
    build_root(tmp_path)
    (tmp_path / "data/w1/current-intake.json").unlink()
    entry = _entry(tmp_path)
    assert entry["expiry"] is None
    assert entry["current_margin_pct"] is None
    assert entry["max_pair_margin_rub"] is None
    assert entry["passes_capital_screen"] is None


def test_catalog_payload_keeps_registry_and_defaults(tmp_path: Path) -> None:
    build_root(tmp_path)
    payload = catalog(
        ScreenerInputs(tmp_path), registry=(PAIR,), funding=StubFunding(), initial_capital=CAPITAL
    )
    assert payload["timeframe"] == "1d"
    assert payload["strategies"] is universe.STRATEGIES
    assert payload["min_daily_volume"] == universe.MIN_DAILY_VOLUME
    assert payload["execution_defaults"] == {"slippage_ticks": 1.0, "quantity": 1}
    assert [entry["id"] for entry in payload["pairs"]] == [PAIR_ID]


@pytest.mark.parametrize("available", [True, False])
def test_catalog_maps_funding_availability_into_strategies(tmp_path: Path, available: bool) -> None:
    build_root(tmp_path)
    funding = StubFunding(available=available)
    payload = catalog(
        ScreenerInputs(tmp_path), registry=(PAIR,), funding=funding, initial_capital=CAPITAL
    )
    entry = payload["pairs"][0]
    assert set(entry) == ENTRY_KEYS
    assert entry["strategies"] == {"mean_reversion": available, "funding_only": available}
    assert len(funding.calls) == 1
