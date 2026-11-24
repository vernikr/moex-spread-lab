"""Registry-backed search keeps legacy family order and fails closed for new engines."""

from types import SimpleNamespace

import pytest

from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE, PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof import proof_batch
from moex_spread_lab.proof import proof_strategy_search as search
from moex_spread_lab.proof.strategy_catalog import get_strategy_registry
from moex_spread_lab.strategies import StrategyRegistry

DEFAULT_REGISTRY = get_strategy_registry()


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
    spec = get_timeframe(timeframe)
    assert search._families({"strategy": active}, targets, spec, DEFAULT_REGISTRY) == expected


def test_discovered_plugin_without_accounting_adapter_is_never_searched():
    fake = SimpleNamespace(
        strategy_id="new_plugin",
        family_id="new_plugin",
        supported_universes=frozenset({PAIR_UNIVERSE}),
        requires_daily_bars=False,
    )
    registry = StrategyRegistry({**DEFAULT_REGISTRY.as_dict(), "new_plugin": fake})
    # Injected registry, not a patched module global: the unmatched plugin is ignored.
    assert search._families(
        {"strategy": "mean_reversion"},
        [{"universe": PAIR_UNIVERSE}],
        get_timeframe("1d"),
        registry,
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
    existing = DEFAULT_REGISTRY.as_dict()
    # The narrow adapter set stays unambiguous per family: the injected plugin joins because it
    # declares the adapter and owns a family of its own, while z-score stays in the shared
    # ``mean_reversion`` family (the pair-screen adapter wins it) and RUONIA keeps its own.
    assert calendar_proof_plugin_ids(
        StrategyRegistry({**existing, plugin.strategy_id: plugin})
    ) == {"new_calendar", "convergence_to_ruonia"}
    # A second adapter in a family that already has one is ambiguous and stays out, which is why
    # the built-in z-score entry (family ``mean_reversion``) does not join the narrow set.
    plugin.family_id = "mean_reversion"
    assert calendar_proof_plugin_ids(
        StrategyRegistry({**existing, plugin.strategy_id: plugin})
    ) == {"convergence_to_ruonia"}
    plugin.family_id = "new_calendar"
    for universes, fills in (
        (frozenset({PAIR_UNIVERSE, CALENDAR_UNIVERSE}), plugin.supported_fills),
        (frozenset({CALENDAR_UNIVERSE}), ("next_open",)),
    ):
        plugin.supported_universes = universes
        plugin.supported_fills = fills
        assert "new_calendar" not in calendar_proof_plugin_ids(
            StrategyRegistry({**existing, plugin.strategy_id: plugin})
        )


def test_injected_adapted_plugin_enters_the_search_without_patching():
    """A drop-in calendar plugin is searched once a registry carrying it is injected."""
    from moex_spread_lab.strategies.calendar_zscore.plugin import CalendarZscorePlugin

    class DropIn(CalendarZscorePlugin):
        strategy_id = "drop_in_calendar"
        family_id = "drop_in_calendar"
        proof_adapter = "calendar_price_intents.v1"

    plugin = DropIn()
    registry = StrategyRegistry({**DEFAULT_REGISTRY.as_dict(), plugin.strategy_id: plugin})
    families = search._families(
        {"strategy": plugin.strategy_id},
        [{"universe": CALENDAR_UNIVERSE}],
        get_timeframe("1d"),
        registry,
    )
    assert families == ["zscore_mean_reversion", "convergence_to_ruonia", "drop_in_calendar"]
