"""Funding intent boundary rejects fictional or ambiguous event histories."""

from copy import deepcopy
from dataclasses import replace

import pytest

from moex_spread_lab.core.contracts import StrategyIntentRun, WindowSnapshot
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.funding_carry.intents import carry_settlement_intents

DAYS = [f"2026-01-0{i}" for i in range(1, 6)]
ROWS = [
    {
        "date": d,
        "near_settle": 100 + i,
        "far_settle": 200 + i,
        "near_volume": 100,
        "far_volume": 100,
    }
    for i, d in enumerate(DAYS)
]
TRADE = {
    "side": "short_spread",
    "entry_date": DAYS[1],
    "exit_date": DAYS[3],
    "signal_date": DAYS[0],
    "reason": "rule_exit",
    "open_at_end": False,
}


class Plugin:
    strategy_id = "carry_test"
    supported_fills = ("same_day_settlement",)

    def __init__(self, items, mutate=False):
        self.items, self.mutate = items, mutate

    def evaluate_intents(self, window, params, fill):
        if self.mutate:
            window.carry_study["rows"][0]["near_settle"] = 0
            params["x"] = 5
        return StrategyIntentRun(self.strategy_id, fill, {"x": 1}, tuple(self.items))


def window(rows=ROWS):
    return WindowSnapshot(
        universe="pair_screen", pair="N/F", near="N", far="F", carry_study={"rows": deepcopy(rows)}
    )


def test_ignores_plugin_money_and_isolates_mutation():
    snap = window()
    original = deepcopy(snap)
    result = carry_settlement_intents(
        Plugin([{**TRADE, "gross_pnl": 10000, "funding": 10000}], True), snap, {"x": 1}
    )
    assert result == [TRADE] and snap == original


@pytest.mark.parametrize(
    "changes",
    [
        {"side": "long_spread"},
        {"signal_date": DAYS[2]},
        {"entry_date": DAYS[3]},
        {"exit_date": "bad"},
        {"open_at_end": 1},
        {"open_at_end": True},
        {"reason": ""},
        {"reason": "open_at_data_end_no_executable_exit"},
    ],
)
def test_bad_event_is_rejected(changes):
    with pytest.raises(W0Error):
        carry_settlement_intents(Plugin([{**TRADE, **changes}]), window(), {"x": 1})


def test_terminal_mark_is_not_an_executable_exit():
    terminal = {
        **TRADE,
        "exit_date": DAYS[-1],
        "reason": "open_at_data_end_no_executable_exit",
        "open_at_end": True,
    }
    rows = deepcopy(ROWS)
    rows[-1]["far_volume"] = 0
    assert carry_settlement_intents(Plugin([terminal]), window(rows), {"x": 1})[0] == terminal


def test_duplicate_overlap_missing_volume_and_bad_row_fail():
    for trades in ([TRADE, TRADE], [TRADE, {**TRADE, "entry_date": DAYS[2], "exit_date": DAYS[4]}]):
        with pytest.raises(W0Error):
            carry_settlement_intents(Plugin(trades), window(), {"x": 1})
    for key, value in (("near_volume", 0), ("far_settle", None)):
        rows = deepcopy(ROWS)
        rows[1][key] = value
        with pytest.raises(W0Error):
            carry_settlement_intents(Plugin([TRADE]), window(rows), {"x": 1})


def test_inconsistent_identity_fails():
    class Wrong(Plugin):
        def evaluate_intents(self, window, params, fill):
            return replace(super().evaluate_intents(window, params, fill), strategy_id="wrong")

    with pytest.raises(W0Error):
        carry_settlement_intents(Wrong([TRADE]), window(), {"x": 1})
