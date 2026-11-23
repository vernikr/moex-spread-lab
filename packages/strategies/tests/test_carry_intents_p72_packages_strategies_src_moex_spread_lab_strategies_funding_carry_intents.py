"""P7.2 strict-typing regression pins for the funding-carry intent boundary.

The module was typed under whole-package strict without behavioral change; these
tests pin the row-shape boundary, the normalized trade payload and the defensive
checks on plugin output that the typing refactor kept in place.
"""

from copy import deepcopy
from typing import Any, cast

import pytest

from moex_spread_lab.core.contracts import StrategyIntentRun, StrategyPlugin, WindowSnapshot
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.funding_carry.intents import carry_settlement_intents

DAYS = [f"2026-02-0{i}" for i in range(1, 6)]
ROWS = [
    {
        "date": day,
        "near_settle": 100 + i,
        "far_settle": 200 + i,
        "near_volume": 100,
        "far_volume": 100,
    }
    for i, day in enumerate(DAYS)
]
TRADE = {
    "side": "short_spread",
    "entry_date": DAYS[1],
    "exit_date": DAYS[3],
    "signal_date": DAYS[0],
    "reason": "rule_exit",
    "open_at_end": False,
}
UNUSABLE_ROWS = "Нет пригодного ряда расчётных цен carry."


class _Plugin:
    """Minimal double: the boundary reads only ``strategy_id`` and ``supported_fills``."""

    strategy_id = "carry_p72"
    supported_fills = ("same_day_settlement",)

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items

    def evaluate_intents(self, window, params, fill):
        return StrategyIntentRun(self.strategy_id, fill, dict(params), tuple(self.items))


def _window(rows: Any) -> WindowSnapshot:
    return WindowSnapshot(
        universe="pair_screen", pair="N/F", near="N", far="F", carry_study={"rows": deepcopy(rows)}
    )


def _run(plugin: Any, rows: Any = ROWS) -> list[dict[str, Any]]:
    return carry_settlement_intents(cast(StrategyPlugin, plugin), _window(rows), {"x": 1})


@pytest.mark.parametrize("rows", [None, [], "rows", [ROWS[0], "row"]])
def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_intents_unusable_rows(
    rows: Any,
) -> None:
    with pytest.raises(W0Error, match=UNUSABLE_ROWS):
        _run(_Plugin([TRADE]), rows)


def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_intents_payload():
    item = {**TRADE, "entry_reason": "signal_threshold", "gross_pnl": 10_000, "funding": 10_000}
    assert _run(_Plugin([item])) == [{**TRADE, "entry_reason": "signal_threshold"}]
    assert _run(_Plugin([TRADE])) == [TRADE]


def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_intents_terminal():
    rows = deepcopy(ROWS)
    rows[-1]["far_volume"] = 0
    terminal = {
        **TRADE,
        "exit_date": DAYS[-1],
        "reason": "open_at_data_end_no_executable_exit",
        "open_at_end": True,
    }
    assert _run(_Plugin([terminal]), rows) == [terminal]


def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_intents_shape():
    class _MutableIntents(_Plugin):
        def evaluate_intents(self, window, params, fill):
            # Emulates a plugin that ignores the declared immutable intent tuple.
            return StrategyIntentRun(
                self.strategy_id,
                fill,
                dict(params),
                cast("tuple[dict[str, Any], ...]", list(self.items)),
            )

    with pytest.raises(W0Error, match="несогласованный"):
        _run(_MutableIntents([TRADE]))


def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_intents_identity():
    class _Foreign(_Plugin):
        def evaluate_intents(self, window, params, fill):
            return StrategyIntentRun("another", fill, dict(params), tuple(self.items))

    with pytest.raises(W0Error, match="несогласованный"):
        _run(_Foreign([TRADE]))
