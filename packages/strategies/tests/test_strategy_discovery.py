"""Built-in plugins are discovered from their folder, so two branches never collide.

The manifest deliberately declares no ``moex_spread_lab.strategies`` entry point for a
workspace strategy: ``discover_strategies()`` walks the subpackages instead, and entry
points stay reserved for external wheels installed outside ``msl-strategies``. Discovery
also must not silently replace a registered implementation.
"""

import argparse
import importlib
import importlib.metadata
import tomllib
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from moex_spread_lab.core.contracts import (
    StrategyPlugin,
    plugin_parameter_aliases,
    plugin_required_asset_capability,
    plugin_trust_tier,
    plugin_ui_field_order,
)
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies import base, pair_fill
from moex_spread_lab.strategies.calendar_zscore.plugin import PLUGIN, CalendarZscorePlugin
from moex_spread_lab.strategies.cli import register as register_strategy_cli

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPO_ROOT / "packages/strategies/pyproject.toml"
STRATEGY_EP_GROUP = "moex_spread_lab.strategies"

BUILTIN_SUBPACKAGES = {
    "mean_reversion": "pair_mean_reversion",
    "funding_only": "funding_carry",
    "zscore_mean_reversion": "calendar_zscore",
    "convergence_to_ruonia": "calendar_ruonia",
}
UI_FIELD_ORDER = {
    "mean_reversion": ("entry_z", "exit_z", "lookback"),
    "zscore_mean_reversion": ("entry_z", "exit_z", "window"),
}


def _registry(plugins: dict[str, Any]) -> base.StrategyRegistry:
    return base.StrategyRegistry(cast("dict[str, StrategyPlugin]", plugins))


class EntryPoint:
    def __init__(self, name, plugin):
        self.name = name
        self.plugin = plugin

    def load(self):
        return self.plugin


def _installed_entry_points() -> list:
    return list(importlib.metadata.entry_points(group=STRATEGY_EP_GROUP))


def _discover_with(extra_plugin: Any, name: str) -> base.StrategyRegistry:
    with patch.object(
        importlib.metadata,
        "entry_points",
        return_value=[*_installed_entry_points(), EntryPoint(name, extra_plugin)],
    ):
        return base.discover_strategies()


@pytest.mark.parametrize(
    ("extra_plugin", "name", "message"),
    [
        (CalendarZscorePlugin(), "zscore_mean_reversion", "Повторяющийся идентификатор стратегии"),
        (object(), "invalid", "не реализует StrategyPlugin"),
    ],
)
def test_invalid_entry_points_are_rejected_instead_of_replaced(extra_plugin, name, message):
    with pytest.raises(W0Error, match=message):
        _discover_with(extra_plugin, name)


def test_repeated_reference_to_same_plugin_is_safe():
    registry = _discover_with(PLUGIN, "another-alias")
    assert registry.get("zscore_mean_reversion") is PLUGIN


def test_empty_strategy_id_is_rejected():
    rival = CalendarZscorePlugin()
    rival.strategy_id = " "
    with pytest.raises(W0Error, match="непустой строковый идентификатор"):
        _discover_with(rival, "empty")


def test_builtins_come_from_subpackages_not_from_entry_points():
    project = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))["project"]
    assert STRATEGY_EP_GROUP not in project.get("entry-points", {})
    installed = {entry.name for entry in importlib.metadata.entry_points(group=STRATEGY_EP_GROUP)}
    assert not installed & set(BUILTIN_SUBPACKAGES)
    registry = base.discover_strategies()
    for strategy_id, subpackage in BUILTIN_SUBPACKAGES.items():
        module = type(registry.get(strategy_id)).__module__
        assert module == f"moex_spread_lab.strategies.{subpackage}.plugin"


def test_builtins_declare_trust_tier_ui_order_and_aliases():
    registry = base.discover_strategies()
    for strategy_id, order in UI_FIELD_ORDER.items():
        plugin = registry.get(strategy_id)
        assert plugin_trust_tier(plugin) == "builtin"
        assert plugin_ui_field_order(plugin, ()) == order
    assert dict(plugin_parameter_aliases(registry.get("mean_reversion"))) == {"window": "lookback"}
    assert dict(plugin_parameter_aliases(registry.get("zscore_mean_reversion"))) == {
        "lookback": "window"
    }
    assert dict(plugin_parameter_aliases(registry.get("funding_only"))) == {}
    ruonia = registry.get("convergence_to_ruonia")
    assert plugin_required_asset_capability(ruonia) == "convergence"
    assert plugin_trust_tier(ruonia) == "builtin"
    assert plugin_required_asset_capability(registry.get("funding_only")) is None


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
    from dataclasses import replace

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


@pytest.mark.parametrize(
    "suffix",
    (
        "analytics",
        "calendar_spreads",
        "carry",
        "funding_recheck",
        "funding_screen",
        "proof_funding",
        "proof_signals",
        "screener",
        "strategies.pair_screener.screener",
    ),
)
def test_retired_strategy_shims_stay_deleted(suffix):
    # Build the name at runtime: these are absence checks, not legacy import edges.
    module = f"moex_spread_lab.{suffix}"
    with pytest.raises(ModuleNotFoundError) as error:
        importlib.import_module(module)
    assert error.value.name == module
