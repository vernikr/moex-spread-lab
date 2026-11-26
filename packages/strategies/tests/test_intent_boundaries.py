"""Fail-closed validation of externally supplied strategy intents.

Every adapter in front of a trusted market window (pair price fills, calendar price fills,
funding settlement events) must recompute money from the window, isolate plugin mutation,
reject fictional or overlapping fills, and refuse a mismatched run identity. The three
families share that contract, so the shared behaviours are tested once against a family
table and each family keeps only its own rules.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace

import pytest

from moex_spread_lab.core.contracts import StrategyIntentRun, WindowSnapshot
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.calendar.intents import calendar_intent_trades
from moex_spread_lab.strategies.funding_carry.intents import carry_settlement_intents
from moex_spread_lab.strategies.pair_mean_reversion import plugin as plugin_module
from moex_spread_lab.strategies.pair_mean_reversion.plugin import PairMeanReversionPlugin
from moex_spread_lab.strategies.pair_screener.intents import pair_price_intent_trades

PAIR_ROWS = tuple(
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
PAIR_CONTRACTS = {
    "A": {"tick_value": 10.0, "tick_size": 1.0},
    "B": {"tick_value": 5.0, "tick_size": 1.0},
}
PAIR_TRADE = {
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
PAIR_INVALID_CHANGES = (
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
)

CAL_ROWS = tuple(
    {
        "date": f"2026-01-0{i}",
        "spread": float(i),
        "open_spread": float(i) + 0.5,
        "signal_eligible": True,
        "entry_eligible": True,
        "exit_eligible": True,
    }
    for i in range(1, 6)
)
CAL_TRADE = {
    "side": "long_spread",
    "entry_date": CAL_ROWS[1]["date"],
    "exit_date": CAL_ROWS[3]["date"],
    "entry_price": 2.5,
    "exit_price": 4.5,
    "signal": {"date": CAL_ROWS[0]["date"], "kind": "custom", "value": 1.0},
    "reason": "exit",
}
CAL_INVALID_CHANGES = (
    {"entry_price": -100},
    {"exit_price": float("nan")},
    {"entry_date": "missing"},
    {"side": "unknown"},
    {"reason": ""},
    {"reason": "end_of_sample"},
    {"signal": {"date": CAL_ROWS[1]["date"], "kind": "x", "value": 1}},
    {"signal": {"date": CAL_ROWS[0]["date"], "kind": "x", "value": float("nan")}},
    {"reason": "end_of_window"},
    {"exit_date": CAL_ROWS[1]["date"], "exit_price": 2.5},
)

CARRY_DAYS = [f"2026-01-0{i}" for i in range(1, 6)]
CARRY_ROWS = [
    {
        "date": day,
        "near_settle": 100 + i,
        "far_settle": 200 + i,
        "near_volume": 100,
        "far_volume": 100,
    }
    for i, day in enumerate(CARRY_DAYS)
]
CARRY_TRADE = {
    "side": "short_spread",
    "entry_date": CARRY_DAYS[1],
    "exit_date": CARRY_DAYS[3],
    "signal_date": CARRY_DAYS[0],
    "reason": "rule_exit",
    "open_at_end": False,
}
CARRY_INVALID_CHANGES = (
    {"side": "long_spread"},
    {"signal_date": CARRY_DAYS[2]},
    {"entry_date": CARRY_DAYS[3]},
    {"exit_date": "bad"},
    {"open_at_end": 1},
    {"open_at_end": True},
    {"reason": ""},
    {"reason": "open_at_data_end_no_executable_exit"},
)


class PairPlugin:
    strategy_id = "pair_example"
    supported_fills = ("next_open", "next_close", "reference_same_bar")

    def __init__(self, items, *, mutate: bool = False) -> None:
        self.items, self.mutate = items, mutate

    def evaluate_intents(self, window, params, fill):
        if self.mutate:
            window.rows[1]["near_open"] = -999
            params["x"] = 999
        return StrategyIntentRun(self.strategy_id, fill, {"x": 1}, tuple(self.items))


class CalendarPlugin:
    strategy_id = "calendar_example"
    supported_fills = ("next_open",)

    def __init__(self, items, *, mutate: bool = False) -> None:
        self.items, self.mutate = items, mutate

    def evaluate_intents(self, window, params, fill):
        if self.mutate:
            window.rows[1]["open_spread"] = -999
            params["value"] = 999
        return StrategyIntentRun(self.strategy_id, fill, {"value": 1}, tuple(self.items))


class CarryPlugin:
    strategy_id = "carry_example"
    supported_fills = ("same_day_settlement",)

    def __init__(self, items, *, mutate: bool = False) -> None:
        self.items, self.mutate = items, mutate

    def evaluate_intents(self, window, params, fill):
        if self.mutate:
            window.carry_study["rows"][0]["near_settle"] = 0
            params["x"] = 5
        return StrategyIntentRun(self.strategy_id, fill, {"x": 1}, tuple(self.items))


def _pair_window(rows, contracts) -> WindowSnapshot:
    return WindowSnapshot(
        universe="pair_screen",
        pair="A/B",
        near="A",
        far="B",
        rows=deepcopy(rows),
        contracts=deepcopy(contracts),
    )


def pair_run(items, rows=PAIR_ROWS, contracts=PAIR_CONTRACTS, mutate=False):
    return pair_price_intent_trades(
        PairPlugin(items, mutate=mutate), _pair_window(rows, contracts), {"x": 1}, "next_open"
    )


def calendar_run(items, rows=CAL_ROWS, mutate=False):
    return calendar_intent_trades(
        CalendarPlugin(items, mutate=mutate),
        WindowSnapshot(universe="calendar_native", pair="A/B", rows=deepcopy(rows)),
        {"value": 1},
        "next_open",
    )


def _carry_window(rows) -> WindowSnapshot:
    return WindowSnapshot(
        universe="pair_screen", pair="N/F", near="N", far="F", carry_study={"rows": deepcopy(rows)}
    )


def carry_run(items, rows=CARRY_ROWS, mutate=False):
    return carry_settlement_intents(
        CarryPlugin(items, mutate=mutate), _carry_window(rows), {"x": 1}
    )


def _pair_identity_mismatch(items):
    class Wrong(PairPlugin):
        def evaluate_intents(self, window, params, fill):
            return replace(super().evaluate_intents(window, params, fill), strategy_id="wrong")

    return pair_price_intent_trades(
        Wrong(items), _pair_window(PAIR_ROWS, PAIR_CONTRACTS), {"x": 1}, "next_open"
    )


def _calendar_identity_mismatch(items):
    class Wrong(CalendarPlugin):
        def evaluate_intents(self, window, params, fill):
            return replace(super().evaluate_intents(window, params, fill), strategy_id="wrong")

    return calendar_intent_trades(
        Wrong(items),
        WindowSnapshot(universe="calendar_native", pair="A/B", rows=CAL_ROWS),
        {"value": 1},
        "next_open",
    )


def _carry_identity_mismatch(items):
    class Wrong(CarryPlugin):
        def evaluate_intents(self, window, params, fill):
            return replace(super().evaluate_intents(window, params, fill), strategy_id="wrong")

    return carry_settlement_intents(
        Wrong(items),
        WindowSnapshot(universe="pair_screen", pair="N/F", carry_study={"rows": CARRY_ROWS}),
        {"x": 1},
    )


@dataclass(frozen=True)
class IntentFamily:
    run: Callable[..., list[dict]]
    rows: tuple[dict, ...] | list[dict]
    trade: dict
    invalid_changes: tuple[dict, ...]
    overlap: dict
    identity_mismatch: Callable[[list[dict]], list[dict]]


FAMILIES = {
    "pair": IntentFamily(
        run=pair_run,
        rows=PAIR_ROWS,
        trade=PAIR_TRADE,
        invalid_changes=PAIR_INVALID_CHANGES,
        overlap={
            **PAIR_TRADE,
            "entry_date": "2026-01-03",
            "entry_near_fill": 103.0,
            "entry_far_fill": 203.0,
            "exit_date": "2026-01-05",
            "exit_near_fill": 105.0,
            "exit_far_fill": 205.0,
        },
        identity_mismatch=_pair_identity_mismatch,
    ),
    "calendar": IntentFamily(
        run=calendar_run,
        rows=CAL_ROWS,
        trade=CAL_TRADE,
        invalid_changes=CAL_INVALID_CHANGES,
        overlap={
            **CAL_TRADE,
            "entry_date": CAL_ROWS[2]["date"],
            "entry_price": 3.5,
            "exit_date": CAL_ROWS[3]["date"],
            "exit_price": 4.5,
        },
        identity_mismatch=_calendar_identity_mismatch,
    ),
    "carry": IntentFamily(
        run=carry_run,
        rows=CARRY_ROWS,
        trade=CARRY_TRADE,
        invalid_changes=CARRY_INVALID_CHANGES,
        overlap={**CARRY_TRADE, "entry_date": CARRY_DAYS[2], "exit_date": CARRY_DAYS[4]},
        identity_mismatch=_carry_identity_mismatch,
    ),
}
FAMILY_CASES = pytest.mark.parametrize("family", FAMILIES.values(), ids=FAMILIES.keys())


@FAMILY_CASES
def test_plugin_mutation_never_reaches_market_data(family):
    rows = deepcopy(family.rows)
    family.run([family.trade], rows=rows, mutate=True)
    assert rows == family.rows


@FAMILY_CASES
def test_invalid_events_fail_closed(family):
    for changes in family.invalid_changes:
        with pytest.raises(W0Error):
            family.run([{**family.trade, **changes}])


@FAMILY_CASES
def test_duplicate_and_overlapping_trades_are_rejected(family):
    with pytest.raises(W0Error):
        family.run([family.trade, family.trade])
    with pytest.raises(W0Error):
        family.run([family.trade, family.overlap])


@FAMILY_CASES
def test_mismatched_run_identity_is_rejected(family):
    with pytest.raises(W0Error):
        family.identity_mismatch([family.trade])


def test_pair_leg_prices_and_point_values_are_recomputed():
    trade = pair_run([PAIR_TRADE])[0]
    assert trade["entry"] == {
        "date": "2026-01-02",
        "near_price": 102.0,
        "far_price": 202.0,
        "spread_fill": 10.0,
    }
    assert trade["exit"]["spread_fill"] == 20.0
    assert trade["signal"]["date"] == "2026-01-01"
    assert not trade["open_at_end"]


def test_pair_missing_prices_volumes_and_cost_basis_fail_closed():
    for key, value in (("near_volume", 0), ("far_volume", None), ("near_open", None)):
        rows = deepcopy(PAIR_ROWS)
        rows[1][key] = value
        with pytest.raises(W0Error):
            pair_run([PAIR_TRADE], rows)
    for tick in (0, -1, None):
        contracts = deepcopy(PAIR_CONTRACTS)
        contracts["A"]["tick_value"] = tick
        with pytest.raises(W0Error):
            pair_run([PAIR_TRADE], contracts=contracts)


def test_pair_forced_end_is_a_close_mark_not_next_open_fill():
    terminal = {
        **PAIR_TRADE,
        "exit_date": "2026-01-05",
        "exit_near_fill": 105.5,
        "exit_far_fill": 205.5,
        "reason": "end_of_sample",
    }
    result = pair_run([terminal])[0]
    assert result["open_at_end"] and result["exit"]["spread_fill"] == 27.5
    with pytest.raises(W0Error):
        pair_run([{**terminal, "exit_near_fill": 105.0, "exit_far_fill": 205.0}])


def test_pair_hourly_timestamps_are_canonical_and_delayed():
    rows = [{**row, "date": f"2026-01-01T{10 + i:02}:00:00"} for i, row in enumerate(PAIR_ROWS)]
    trade = {
        **PAIR_TRADE,
        "entry_date": "2026-01-01 11:00:00",
        "exit_date": "2026-01-01T13:00:00",
        "signal": {"date": "2026-01-01T10:00:00", "kind": "z", "value": 1.0},
    }

    def run_hourly(items):
        return pair_price_intent_trades(
            PairPlugin(items),
            WindowSnapshot(
                universe="pair_screen",
                pair="A/B",
                near="A",
                far="B",
                timeframe="1h",
                rows=rows,
                contracts=PAIR_CONTRACTS,
            ),
            {"x": 1},
            "next_open",
        )

    assert run_hourly([trade])[0]["entry"]["date"] == "2026-01-01T11:00:00"
    with pytest.raises(W0Error):
        run_hourly([{**trade, "signal": {"date": "2026-01-01T11:00:00", "kind": "z", "value": 1}}])


def test_calendar_intent_rebinds_to_market_rows():
    trade = calendar_run([CAL_TRADE])[0]
    assert trade["direction"] == 1
    assert trade["entry"]["date"] == CAL_TRADE["entry_date"]
    assert trade["entry_price"] == 2.5


def test_calendar_ineligible_rows_rules():
    rows = deepcopy(CAL_ROWS)
    rows[1]["entry_eligible"] = False
    with pytest.raises(W0Error):
        calendar_run([CAL_TRADE], rows)
    rows = deepcopy(CAL_ROWS)
    rows[3]["exit_eligible"] = False
    with pytest.raises(W0Error):
        calendar_run([CAL_TRADE], rows)
    terminal_rows = deepcopy(CAL_ROWS)
    terminal_rows[-1]["exit_eligible"] = False
    terminal = {
        **CAL_TRADE,
        "exit_date": terminal_rows[-1]["date"],
        "exit_price": 5.5,
        "reason": "end_of_window",
    }
    assert calendar_run([terminal], terminal_rows)[0]["reason"] == "end_of_window"


def test_calendar_nonmonotonic_rows_are_rejected():
    with pytest.raises(W0Error):
        calendar_run([CAL_TRADE], tuple(reversed(CAL_ROWS)))


def test_calendar_open_at_end_mark_is_accepted_only_on_the_final_bar():
    """A position opened on the last bar is reported open, not closed on the same day."""
    rows = deepcopy(CAL_ROWS)
    terminal = {
        **CAL_TRADE,
        "entry_date": rows[-1]["date"],
        "exit_date": rows[-1]["date"],
        "entry_price": 5.5,
        "exit_price": 5.5,
        "reason": "end_of_window",
        "open_at_end": True,
    }
    assert calendar_run([terminal], rows)[0]["entry"]["date"] == rows[-1]["date"]
    with pytest.raises(W0Error):
        # Not the final row: an open-at-end marker must sit on the window's last bar.
        calendar_run(
            [{**terminal, "entry_date": rows[2]["date"], "exit_date": rows[2]["date"]}], rows
        )


def test_carry_ignores_plugin_money_and_isolates_mutation():
    snap = WindowSnapshot(
        universe="pair_screen",
        pair="N/F",
        near="N",
        far="F",
        carry_study={"rows": deepcopy(CARRY_ROWS)},
    )
    original = deepcopy(snap)
    trade = {**CARRY_TRADE, "gross_pnl": 10000, "funding": 10000}
    assert carry_settlement_intents(CarryPlugin([trade], mutate=True), snap, {"x": 1}) == [
        CARRY_TRADE
    ]
    assert snap == original


def test_carry_terminal_mark_is_not_an_executable_exit():
    terminal = {
        **CARRY_TRADE,
        "exit_date": CARRY_DAYS[-1],
        "reason": "open_at_data_end_no_executable_exit",
        "open_at_end": True,
    }
    rows = deepcopy(CARRY_ROWS)
    rows[-1]["far_volume"] = 0
    assert carry_settlement_intents(CarryPlugin([terminal]), _carry_window(rows), {"x": 1}) == [
        terminal
    ]


def test_carry_missing_volume_and_bad_row_fail():
    for key, value in (("near_volume", 0), ("far_settle", None)):
        rows = deepcopy(CARRY_ROWS)
        rows[1][key] = value
        with pytest.raises(W0Error):
            carry_run([CARRY_TRADE], rows)


def test_pair_plugin_signal_day_uses_positive_volume_rows(monkeypatch):
    rows = [
        {
            "date": "2026-01-01",
            "near_volume": 10,
            "far_volume": 10,
            "near_open": 100,
            "far_open": 90,
        },
        {
            "date": "2026-01-02",
            "near_volume": 0,
            "far_volume": 10,
            "near_open": 101,
            "far_open": 91,
        },
        {
            "date": "2026-01-03",
            "near_volume": 10,
            "far_volume": 10,
            "near_open": 102,
            "far_open": 92,
        },
        {
            "date": "2026-01-04",
            "near_volume": 10,
            "far_volume": 10,
            "near_open": 103,
            "far_open": 93,
        },
    ]
    contracts = {
        "NEAR": {"tick_size": 1, "tick_value": 1, "lot_size": 1, "initial_margin": 1},
        "FAR": {"tick_size": 1, "tick_value": 1, "lot_size": 1, "initial_margin": 1},
    }

    def fake_baseline(series, **options):
        assert [row["date"] for row in series["rows"]] == ["2026-01-01", "2026-01-03", "2026-01-04"]
        return {
            "trades": [
                {
                    "direction": "long_near_short_far",
                    "entry_date": "2026-01-03",
                    "exit_date": "2026-01-04",
                    "entry_near_fill": 102,
                    "entry_far_fill": 92,
                    "exit_near_fill": 103,
                    "exit_far_fill": 93,
                    "entry_signal_z": 2.0,
                    "exit_reason": "mean_reversion_exit",
                }
            ]
        }

    monkeypatch.setattr(plugin_module, "run_mean_reversion_baseline", fake_baseline)
    window = WindowSnapshot(
        universe="pair_screen",
        pair="NEAR/FAR",
        near="NEAR",
        far="FAR",
        rows=rows,
        contracts=contracts,
    )
    result = PairMeanReversionPlugin().evaluate_intents(
        window, {"lookback": 5, "entry_z": 2.0, "exit_z": 0.5}, "next_open"
    )
    assert result.intents[0]["signal"]["date"] == "2026-01-01"
