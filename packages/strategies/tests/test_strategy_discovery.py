"""Discovery must not silently replace a registered strategy implementation."""

from unittest.mock import patch

import pytest

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies import base
from moex_spread_lab.strategies.calendar_zscore.plugin import CalendarZscorePlugin, PLUGIN


class EntryPoint:
    def __init__(self, name, plugin):
        self.name = name
        self.plugin = plugin

    def load(self):
        return self.plugin


def test_repeated_reference_to_same_plugin_is_safe():
    original = list(base.importlib.metadata.entry_points(group="moex_spread_lab.strategies"))
    with patch.object(base.importlib.metadata, "entry_points", return_value=[*original, EntryPoint("another-alias", PLUGIN)]):
        registry = base.discover_strategies()
    assert registry.get("zscore_mean_reversion") is PLUGIN


def test_conflicting_strategy_id_is_rejected_instead_of_replaced():
    original = list(base.importlib.metadata.entry_points(group="moex_spread_lab.strategies"))
    rival = CalendarZscorePlugin()
    with patch.object(base.importlib.metadata, "entry_points", return_value=[*original, EntryPoint("zscore_mean_reversion", rival)]):
        with pytest.raises(W0Error, match="Повторяющийся идентификатор стратегии"):
            base.discover_strategies()


def test_empty_strategy_id_is_rejected():
    original = list(base.importlib.metadata.entry_points(group="moex_spread_lab.strategies"))
    rival = CalendarZscorePlugin()
    rival.strategy_id = " "
    with patch.object(base.importlib.metadata, "entry_points", return_value=[*original, EntryPoint("empty", rival)]):
        with pytest.raises(W0Error, match="непустой строковый идентификатор"):
            base.discover_strategies()


def test_registered_entry_point_must_implement_protocol():
    original = list(base.importlib.metadata.entry_points(group="moex_spread_lab.strategies"))
    with patch.object(base.importlib.metadata, "entry_points", return_value=[*original, EntryPoint("invalid", object())]):
        with pytest.raises(W0Error, match="не реализует StrategyPlugin"):
            base.discover_strategies()
