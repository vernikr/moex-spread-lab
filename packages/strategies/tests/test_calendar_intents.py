"""Fail-closed validation of externally supplied calendar price intents."""

from copy import deepcopy
from dataclasses import replace

import pytest

from moex_spread_lab.core.contracts import StrategyIntentRun, WindowSnapshot
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.calendar.intents import calendar_intent_trades

ROWS = tuple(
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
TRADE = {
    "side": "long_spread",
    "entry_date": ROWS[1]["date"],
    "exit_date": ROWS[3]["date"],
    "entry_price": 2.5,
    "exit_price": 4.5,
    "signal": {"date": ROWS[0]["date"], "kind": "custom", "value": 1.0},
    "reason": "exit",
}


class Plugin:
    strategy_id = "example"
    supported_fills = ("next_open",)

    def __init__(self, items, *, mutate=False):
        self.items = items
        self.mutate = mutate

    def evaluate_intents(self, window, params, fill):
        if self.mutate:
            window.rows[1]["open_spread"] = -999
            params["value"] = 999
        return StrategyIntentRun(self.strategy_id, fill, {"value": 1}, tuple(self.items))


def run(items, rows=ROWS, **kwargs):
    return calendar_intent_trades(
        Plugin(items, **kwargs),
        WindowSnapshot(universe="calendar_native", pair="A/B", rows=deepcopy(rows)),
        {"value": 1},
        "next_open",
    )


def test_valid_intent_rebinds_to_market_rows():
    trade = run([TRADE])[0]
    assert trade["direction"] == 1
    assert trade["entry"]["date"] == TRADE["entry_date"]
    assert trade["entry_price"] == 2.5


def test_plugin_mutation_does_not_change_market_data_or_parameters():
    rows = deepcopy(ROWS)
    trade = run([TRADE], rows, mutate=True)[0]
    assert trade["entry"]["open_spread"] == 2.5
    assert rows == ROWS


@pytest.mark.parametrize(
    "changes",
    [
        {"entry_price": -100},
        {"exit_price": float("nan")},
        {"entry_date": "missing"},
        {"side": "unknown"},
        {"reason": ""},
        {"reason": "end_of_sample"},
        {"signal": {"date": ROWS[1]["date"], "kind": "x", "value": 1}},
        {"signal": {"date": ROWS[0]["date"], "kind": "x", "value": float("nan")}},
        {"reason": "end_of_window"},
        {"exit_date": ROWS[1]["date"], "exit_price": 2.5},
    ],
)
def test_invalid_intents_are_rejected(changes):
    with pytest.raises(W0Error):
        run([{**TRADE, **changes}])


def test_overlap_duplicate_and_ineligible_are_rejected():
    duplicate = deepcopy(TRADE)
    with pytest.raises(W0Error):
        run([TRADE, duplicate])
    rows = deepcopy(ROWS)
    rows[1]["entry_eligible"] = False
    with pytest.raises(W0Error):
        run([TRADE], rows)
    rows = deepcopy(ROWS)
    rows[3]["exit_eligible"] = False
    with pytest.raises(W0Error):
        run([TRADE], rows)


def test_terminal_mark_can_be_ineligible_only_at_last_row():
    rows = deepcopy(ROWS)
    rows[-1]["exit_eligible"] = False
    terminal = {
        **TRADE,
        "exit_date": rows[-1]["date"],
        "exit_price": 5.5,
        "reason": "end_of_window",
    }
    assert run([terminal], rows)[0]["reason"] == "end_of_window"


def test_nonmonotonic_rows_and_wrong_identity_are_rejected():
    with pytest.raises(W0Error):
        run([TRADE], tuple(reversed(ROWS)))

    class Wrong(Plugin):
        def evaluate_intents(self, window, params, fill):
            return replace(super().evaluate_intents(window, params, fill), strategy_id="wrong")

    with pytest.raises(W0Error):
        calendar_intent_trades(
            Wrong([TRADE]),
            WindowSnapshot(universe="calendar_native", pair="A/B", rows=ROWS),
            {"value": 1},
            "next_open",
        )


def test_open_at_end_terminal_mark_is_accepted_only_on_the_final_bar():
    """A position opened on the last bar is reported open, not closed on the same day."""
    rows = deepcopy(ROWS)
    terminal = {
        **TRADE,
        "entry_date": rows[-1]["date"],
        "exit_date": rows[-1]["date"],
        "entry_price": 5.5,
        "exit_price": 5.5,
        "reason": "end_of_window",
        "open_at_end": True,
    }
    trade = run([terminal], rows)[0]
    assert trade["entry"]["date"] == rows[-1]["date"]

    with pytest.raises(W0Error):
        # Not the final row: an open-at-end marker must sit on the window's last bar.
        run([{**terminal, "entry_date": rows[2]["date"], "exit_date": rows[2]["date"]}], rows)
