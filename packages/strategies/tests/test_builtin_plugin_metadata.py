"""Built-in plugins declare the adapter metadata that replaces hardcoded switchboards."""

from __future__ import annotations

from moex_spread_lab.core.contracts import (
    plugin_parameter_aliases,
    plugin_required_asset_capability,
    plugin_trust_tier,
    plugin_ui_field_order,
)
from moex_spread_lab.strategies import discover_strategies


def test_builtins_declare_trust_tier_and_ui_order() -> None:
    registry = discover_strategies()
    expected_order = {
        "mean_reversion": ("entry_z", "exit_z", "lookback"),
        "zscore_mean_reversion": ("entry_z", "exit_z", "window"),
    }
    for strategy_id, order in expected_order.items():
        plugin = registry.get(strategy_id)
        assert plugin_trust_tier(plugin) == "builtin"
        assert plugin_ui_field_order(plugin, ()) == order


def test_family_parameter_aliases_match_historical_translation() -> None:
    registry = discover_strategies()
    assert dict(plugin_parameter_aliases(registry.get("mean_reversion"))) == {"window": "lookback"}
    assert dict(plugin_parameter_aliases(registry.get("zscore_mean_reversion"))) == {
        "lookback": "window"
    }
    assert dict(plugin_parameter_aliases(registry.get("funding_only"))) == {}


def test_convergence_strategy_declares_asset_capability() -> None:
    registry = discover_strategies()
    plugin = registry.get("convergence_to_ruonia")
    assert plugin_required_asset_capability(plugin) == "convergence"
    assert plugin_trust_tier(plugin) == "builtin"
    assert plugin_required_asset_capability(registry.get("funding_only")) is None
