"""Discovery must not silently replace a registered strategy implementation."""

import argparse
import importlib
import importlib.metadata
from typing import Any, cast
from unittest.mock import patch

import pytest

from moex_spread_lab.core.contracts import StrategyPlugin
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies import base, pair_fill
from moex_spread_lab.strategies.calendar_zscore.plugin import PLUGIN, CalendarZscorePlugin
from moex_spread_lab.strategies.cli import register as register_strategy_cli


def _registry(plugins: dict[str, Any]) -> base.StrategyRegistry:
    return base.StrategyRegistry(cast("dict[str, StrategyPlugin]", plugins))


class EntryPoint:
    def __init__(self, name, plugin):
        self.name = name
        self.plugin = plugin

    def load(self):
        return self.plugin


def test_repeated_reference_to_same_plugin_is_safe():
    original = list(importlib.metadata.entry_points(group="moex_spread_lab.strategies"))
    with patch.object(
        importlib.metadata,
        "entry_points",
        return_value=[*original, EntryPoint("another-alias", PLUGIN)],
    ):
        registry = base.discover_strategies()
    assert registry.get("zscore_mean_reversion") is PLUGIN


def test_conflicting_strategy_id_is_rejected_instead_of_replaced():
    original = list(importlib.metadata.entry_points(group="moex_spread_lab.strategies"))
    rival = CalendarZscorePlugin()
    with (
        patch.object(
            importlib.metadata,
            "entry_points",
            return_value=[*original, EntryPoint("zscore_mean_reversion", rival)],
        ),
        pytest.raises(W0Error, match="Повторяющийся идентификатор стратегии"),
    ):
        base.discover_strategies()


def test_empty_strategy_id_is_rejected():
    original = list(importlib.metadata.entry_points(group="moex_spread_lab.strategies"))
    rival = CalendarZscorePlugin()
    rival.strategy_id = " "
    with (
        patch.object(
            importlib.metadata,
            "entry_points",
            return_value=[*original, EntryPoint("empty", rival)],
        ),
        pytest.raises(W0Error, match="непустой строковый идентификатор"),
    ):
        base.discover_strategies()


def test_registered_entry_point_must_implement_protocol():
    original = list(importlib.metadata.entry_points(group="moex_spread_lab.strategies"))
    with (
        patch.object(
            importlib.metadata,
            "entry_points",
            return_value=[*original, EntryPoint("invalid", object())],
        ),
        pytest.raises(W0Error, match="не реализует StrategyPlugin"),
    ):
        base.discover_strategies()


def test_daily_pair_adapter_opt_in_rejects_ambiguous_or_unsupported_plugins():
    from moex_spread_lab.strategies.pair_mean_reversion.plugin import PairMeanReversionPlugin

    class Demo(PairMeanReversionPlugin):
        strategy_id = "custom_pair"
        family_id = "custom_family"
        proof_adapter: Any = "daily_pair_price_intents.v1"
        requires_daily_bars = True

    good = Demo()
    assert base.daily_pair_proof_plugin_ids(_registry({good.strategy_id: good})) == {"custom_pair"}
    for attr, value in (
        ("requires_daily_bars", False),
        ("supported_fills", ("next_open",)),
        ("supported_universes", frozenset({"pair_screen", "calendar_native"})),
        ("proof_adapter", "other"),
    ):
        bad = Demo()
        setattr(bad, attr, value)
        assert not base.daily_pair_proof_plugin_ids(_registry({bad.strategy_id: bad}))
    duplicate = Demo()
    duplicate.strategy_id = "another"
    assert not base.daily_pair_proof_plugin_ids(
        _registry({good.strategy_id: good, duplicate.strategy_id: duplicate})
    )


def test_hourly_pair_adapter_requires_explicit_hourly_capability():
    from moex_spread_lab.strategies.pair_mean_reversion.plugin import PairMeanReversionPlugin

    class Demo(PairMeanReversionPlugin):
        strategy_id = "hourly_pair"
        family_id = "hourly_family"
        proof_adapter = "pair_price_intents.v1"
        requires_daily_bars = False

    plugin = Demo()
    registry = _registry({plugin.strategy_id: plugin})
    assert base.daily_pair_proof_plugin_ids(registry) == {"hourly_pair"}
    plugin.requires_daily_bars = True
    assert not base.daily_pair_proof_plugin_ids(registry)


def test_carry_proof_adapter_requires_unique_daily_pair_settlement_family():
    from moex_spread_lab.strategies.funding_carry.plugin import FundingCarryPlugin

    class Good(FundingCarryPlugin):
        strategy_id = "custom_carry"
        family_id = "custom_carry"
        proof_adapter = "carry_settlement_intents.v1"

    good = Good()
    assert base.carry_proof_plugin_ids(_registry({good.strategy_id: good})) == {"custom_carry"}
    for attr, value in (
        ("requires_daily_bars", False),
        ("supported_fills", ("next_close",)),
        ("supported_universes", frozenset({"calendar_native"})),
        ("proof_adapter", None),
    ):
        bad = Good()
        setattr(bad, attr, value)
        assert not base.carry_proof_plugin_ids(_registry({bad.strategy_id: bad}))
    from dataclasses import replace

    class FixedAxis(Good):
        def parameter_schema(self, context=None):
            return tuple(replace(spec, tunable=False) for spec in super().parameter_schema(context))

    assert not base.carry_proof_plugin_ids(_registry({"fixed": FixedAxis()}))
    duplicate = Good()
    duplicate.strategy_id = "other_carry"
    assert not base.carry_proof_plugin_ids(
        _registry({good.strategy_id: good, duplicate.strategy_id: duplicate})
    )


def test_retired_exports_and_s3_reference_stay_deleted():
    assert not hasattr(base, "importlib")
    assert "importlib" not in base.__all__
    assert not hasattr(base.StrategyRegistry, "for_universe")
    assert not hasattr(base.StrategyRegistry, "items")
    registry = base.discover_strategies()
    assert list(registry.as_dict()) == list(registry.ids())
    assert "s3_reference" not in registry
    for module in (
        "moex_spread_lab.strategies.s3_reference",
        "moex_spread_lab.strategies.s3_reference.engine",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_strategy_cli(subparsers)
    assert "s3-reference" not in subparsers.choices


def test_pair_fill_defaults_and_rejections():
    assert pair_fill({}, "funding_only") == "same_day_settlement"
    assert pair_fill({}, "mean_reversion", default_price_fill="next_open") == "next_open"
    assert pair_fill({"fill": "next_close"}, "unknown_price_strategy") == "next_close"
    for bad in ("next_open", "invalid", None):
        with pytest.raises(W0Error):
            pair_fill({"fill": bad}, "funding_only")
    with pytest.raises(W0Error):
        pair_fill({"fill": "same_day_settlement"}, "mean_reversion")
