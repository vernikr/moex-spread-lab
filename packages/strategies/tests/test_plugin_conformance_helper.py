"""The plugin-owned conformance helper must detect common invalid strategies."""

import pytest

from moex_spread_lab.core.contracts import EntryBandPoint, StrategyIntentRun, WindowSnapshot
from moex_spread_lab.strategies.conformance import assert_causal_conformance


class FixturePlugin:
    strategy_id = "fixture"
    supported_fills = ("next_open",)

    def __init__(self, *, lookahead=False, nondeterministic=False):
        self.lookahead = lookahead
        self.nondeterministic = nondeterministic
        self.calls = 0

    def evaluate_intents(self, window, params, fill):
        self.calls += 1
        value = self.calls if self.nondeterministic else 1
        return StrategyIntentRun(
            self.strategy_id,
            fill,
            {},
            (
                {
                    "side": "long_spread",
                    "entry_date": "2026-01-01",
                    "exit_date": "2026-01-02",
                    "reason": "exit",
                    "value": value,
                },
            ),
        )

    def compute_entry_bands(self, series, params, fill, *, far_point_value_rub=1.0):
        value = len(series) if self.lookahead else 1
        return [EntryBandPoint(date=r["date"], buy_level=value) for r in series]


def fixture():
    rows = [{"date": f"2026-01-{i:02}"} for i in range(1, 9)]
    return WindowSnapshot(universe="calendar_native", pair="A/B", rows=rows), rows


def test_causal_fixture_passes():
    window, rows = fixture()
    assert_causal_conformance(FixturePlugin(), window, rows, {}, cutoffs=(4, 6))


def test_future_dependent_bands_fail():
    window, rows = fixture()
    with pytest.raises(AssertionError, match="future-dependent bands"):
        assert_causal_conformance(FixturePlugin(lookahead=True), window, rows, {}, cutoffs=(4,))


def test_nondeterministic_intents_fail():
    window, rows = fixture()
    with pytest.raises(AssertionError, match="nondeterministic intents"):
        assert_causal_conformance(
            FixturePlugin(nondeterministic=True), window, rows, {}, cutoffs=(4,)
        )
