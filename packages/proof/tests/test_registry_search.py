"""Registry-backed search keeps legacy family order and fails closed for new engines."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE, PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime import proof_batch
from moex_spread_lab.proof_runtime import proof_strategy_search as search
from moex_spread_lab.strategies import StrategyRegistry


@pytest.mark.parametrize(
    ("universes", "active", "timeframe", "expected"),
    [
        ([PAIR_UNIVERSE], "mean_reversion", "1d", ["mean_reversion", "funding_only"]),
        ([PAIR_UNIVERSE], "mean_reversion", "1h", ["mean_reversion"]),
        (
            [CALENDAR_UNIVERSE],
            "zscore_mean_reversion",
            "1d",
            ["zscore_mean_reversion", "convergence_to_ruonia"],
        ),
        ([PAIR_UNIVERSE, CALENDAR_UNIVERSE], "funding_only", "1d", ["mean_reversion"]),
        (
            [PAIR_UNIVERSE, CALENDAR_UNIVERSE],
            "convergence_to_ruonia",
            "1d",
            ["zscore_mean_reversion"],
        ),
    ],
)
def test_family_order_and_mixed_adapter(universes, active, timeframe, expected):
    targets = [{"universe": universe} for universe in universes]
    assert search._families({"strategy": active}, targets, get_timeframe(timeframe)) == expected


def test_discovered_plugin_without_accounting_adapter_is_never_searched():
    fake = SimpleNamespace(
        strategy_id="new_plugin",
        family_id="new_plugin",
        supported_universes=frozenset({PAIR_UNIVERSE}),
        requires_daily_bars=False,
    )
    registry = StrategyRegistry({**search.STRATEGY_REGISTRY.as_dict(), "new_plugin": fake})
    with patch.object(search, "STRATEGY_REGISTRY", registry):
        assert search._families(
            {"strategy": "mean_reversion"}, [{"universe": PAIR_UNIVERSE}], get_timeframe("1d")
        ) == ["mean_reversion", "funding_only"]
    with pytest.raises(W0Error, match="Неизвестная стратегия"):
        proof_batch._native_request({"strategy": "new_plugin"}, {"universe": PAIR_UNIVERSE})


def test_seed_defaults_come_from_contextual_plugin_schema():
    assert (
        proof_batch._active_seed({"asset": "BR"}, "zscore_mean_reversion")["window"]
        != proof_batch._active_seed({"asset": "SR"}, "zscore_mean_reversion")["window"]
    )
    assert proof_batch._active_seed({}, "funding_only")["lookback"] == 20


def test_calendar_proof_opt_in_is_narrow_and_unambiguous():
    from moex_spread_lab.strategies.base import calendar_proof_plugin_ids
    from moex_spread_lab.strategies.calendar_zscore.plugin import CalendarZscorePlugin

    class NewCalendar(CalendarZscorePlugin):
        strategy_id = "new_calendar"
        family_id = "new_calendar"
        proof_adapter = "calendar_price_intents.v1"

    plugin = NewCalendar()
    existing = search.STRATEGY_REGISTRY.as_dict()
    assert calendar_proof_plugin_ids(
        StrategyRegistry({**existing, plugin.strategy_id: plugin})
    ) == {"new_calendar"}
    plugin.family_id = "mean_reversion"
    assert not calendar_proof_plugin_ids(StrategyRegistry({**existing, plugin.strategy_id: plugin}))
    plugin.family_id = "new_calendar"
    plugin.supported_universes = frozenset({PAIR_UNIVERSE, CALENDAR_UNIVERSE})
    assert not calendar_proof_plugin_ids(StrategyRegistry({**existing, plugin.strategy_id: plugin}))
    plugin.supported_universes = frozenset({CALENDAR_UNIVERSE})
    plugin.supported_fills = ("next_open",)
    assert not calendar_proof_plugin_ids(StrategyRegistry({**existing, plugin.strategy_id: plugin}))
