"""The future pair adapter must reject fictional or overlapping proxy fills."""

from copy import deepcopy
from dataclasses import replace

import pytest

from moex_spread_lab.core.contracts import StrategyIntentRun, WindowSnapshot
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.pair_screener.intents import pair_price_intent_trades

ROWS = tuple(
    {
        "date": f"2026-01-0{i}",
        "near_volume": 150,
        "far_volume": 160,
        "near_open": 100.0 + i,
        "far_open": 200.0 + i,
        "near_close": 100.5 + i,
        "far_close": 200.5 + i,
    }
    for i in range(1, 6)
)
CONTRACTS = {
    "A": {"tick_value": 10.0, "tick_size": 1.0},
    "B": {"tick_value": 5.0, "tick_size": 1.0},
}
TRADE = {
    "side": "long_spread",
    "entry_date": "2026-01-02 00:00:00",
    "exit_date": "2026-01-04",
    "entry_near_fill": 102.0,
    "entry_far_fill": 202.0,
    "exit_near_fill": 104.0,
    "exit_far_fill": 204.0,
    "signal": {"date": "2026-01-01", "kind": "custom", "value": 1.0},
    "reason": "rule_exit",
}


class Plugin:
    strategy_id = "pair_example"
    supported_fills = ("next_open", "next_close", "reference_same_bar")

    def __init__(self, items, *, mutate=False):
        self.items, self.mutate = items, mutate

    def evaluate_intents(self, window, params, fill):
        if self.mutate:
            window.rows[1]["near_open"] = -999
            params["x"] = 999
        return StrategyIntentRun(self.strategy_id, fill, {"x": 1}, tuple(self.items))


def run(items, rows=ROWS, contracts=CONTRACTS, **kwargs):
    return pair_price_intent_trades(
        Plugin(items, **kwargs),
        WindowSnapshot(
            universe="pair_screen",
            pair="A/B",
            near="A",
            far="B",
            rows=deepcopy(rows),
            contracts=deepcopy(contracts),
        ),
        {"x": 1},
        "next_open",
    )


def test_leg_prices_and_point_values_are_recomputed():
    trade = run([TRADE])[0]
    assert trade["entry"] == {
        "date": "2026-01-02",
        "near_price": 102.0,
        "far_price": 202.0,
        "spread_fill": 10.0,
    }
    assert trade["exit"]["spread_fill"] == 20.0
    assert trade["signal"]["date"] == "2026-01-01"
    assert not trade["open_at_end"]


def test_plugin_cannot_mutate_trusted_market_data():
    rows = deepcopy(ROWS)
    assert run([TRADE], rows, mutate=True)[0]["entry"]["near_price"] == 102.0
    assert rows == ROWS


@pytest.mark.parametrize(
    "changes",
    [
        {"entry_near_fill": 0},
        {"exit_far_fill": 99},
        {"entry_far_fill": float("nan")},
        {"entry_date": "2026-01-02 12:00:00"},
        {"entry_date": "2026-99-99"},
        {"side": "other"},
        {"reason": ""},
        {"reason": "end_of_window"},
        {"signal": {"date": "2026-01-02", "kind": "x", "value": 1}},
        {"signal": {"date": "2026-01-01", "kind": "x", "value": float("inf")}},
        {"exit_date": "2026-01-02", "exit_near_fill": 102, "exit_far_fill": 202},
        {"reason": "end_of_sample"},
    ],
)
def test_invalid_events_fail_closed(changes):
    with pytest.raises(W0Error):
        run([{**TRADE, **changes}])


def test_missing_prices_volumes_and_cost_basis_fail_closed():
    for key, value in (("near_volume", 0), ("far_volume", None), ("near_open", None)):
        rows = deepcopy(ROWS)
        rows[1][key] = value
        with pytest.raises(W0Error):
            run([TRADE], rows)
    for tick in (0, -1, None):
        contracts = deepcopy(CONTRACTS)
        contracts["A"]["tick_value"] = tick
        with pytest.raises(W0Error):
            run([TRADE], contracts=contracts)


def test_forced_end_is_a_close_mark_not_next_open_fill():
    terminal = {
        **TRADE,
        "exit_date": "2026-01-05",
        "exit_near_fill": 105.5,
        "exit_far_fill": 205.5,
        "reason": "end_of_sample",
    }
    result = run([terminal])[0]
    assert result["open_at_end"] and result["exit"]["spread_fill"] == 27.5
    with pytest.raises(W0Error):
        run([{**terminal, "exit_near_fill": 105.0, "exit_far_fill": 205.0}])


def test_duplicate_and_overlapping_trades_are_rejected():
    with pytest.raises(W0Error):
        run([TRADE, TRADE])
    overlapping = {
        **TRADE,
        "entry_date": "2026-01-03",
        "entry_near_fill": 103.0,
        "entry_far_fill": 203.0,
        "exit_date": "2026-01-05",
        "exit_near_fill": 105.0,
        "exit_far_fill": 205.0,
    }
    with pytest.raises(W0Error):
        run([TRADE, overlapping])


def test_mismatched_run_identity_is_rejected():
    class Wrong(Plugin):
        def evaluate_intents(self, window, params, fill):
            return replace(super().evaluate_intents(window, params, fill), strategy_id="wrong")

    with pytest.raises(W0Error):
        pair_price_intent_trades(
            Wrong([TRADE]),
            WindowSnapshot(
                universe="pair_screen",
                pair="A/B",
                near="A",
                far="B",
                rows=ROWS,
                contracts=CONTRACTS,
            ),
            {"x": 1},
            "next_open",
        )


def test_hourly_timestamps_are_canonical_and_delayed():
    rows = [{**r, "date": f"2026-01-01T{10 + i:02}:00:00"} for i, r in enumerate(ROWS)]
    trade = {
        **TRADE,
        "entry_date": "2026-01-01 11:00:00",
        "exit_date": "2026-01-01T13:00:00",
        "signal": {"date": "2026-01-01T10:00:00", "kind": "z", "value": 1.0},
    }
    result = pair_price_intent_trades(
        Plugin([trade]),
        WindowSnapshot(
            universe="pair_screen",
            pair="A/B",
            near="A",
            far="B",
            timeframe="1h",
            rows=rows,
            contracts=CONTRACTS,
        ),
        {"x": 1},
        "next_open",
    )
    assert result[0]["entry"]["date"] == "2026-01-01T11:00:00"
    with pytest.raises(W0Error):
        pair_price_intent_trades(
            Plugin([{**trade, "signal": {"date": "2026-01-01T11:00:00", "kind": "z", "value": 1}}]),
            WindowSnapshot(
                universe="pair_screen",
                pair="A/B",
                near="A",
                far="B",
                timeframe="1h",
                rows=rows,
                contracts=CONTRACTS,
            ),
            {"x": 1},
            "next_open",
        )
