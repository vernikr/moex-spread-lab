"""Independent LQDT account reconstruction shared by the global-capital tests.

The runtime publishes a self-financing ledger; ``test_global_capital.py`` rebuilds
the same account outside the runtime so an annualized score can be compared with an
independent expectation. Wealth grows only the part of the envelope not held as
margin, adds each row's incremental net P&L, and holds the published margin under
automatic sizing or the trade-summed margin under explicit sizing. Nothing restarts
at the evaluation-split boundary.
"""

import json
from datetime import date
from pathlib import Path

from moex_spread_lab.core.settings import EvaluationSplit
from moex_spread_lab.proof.proof_search import _annualized_returns

LINEAGE_PATH = Path("data/benchmark/lqdt-lineage.json")


def published_marks(root, ledger):
    """LQDT closes for exactly the dates the proof ledger reports."""
    source = json.loads((root / LINEAGE_PATH).read_text())
    prices = {row["date"]: row["price"] for row in source["rows"]}
    return {row["date"]: prices[row["date"]] for row in ledger}


def held_margin(row, trades, automatic):
    """Margin posted at the end of one ledger row."""
    if automatic:
        return row["posted_margin_rub"]
    return sum(
        trade["margin_rub"]
        for trade in trades
        if trade["entry"]["date"] <= row["date"] < trade["exit"]["date"]
    )


def reconstruct_wealth(ledger, marks, trades, initial, automatic):
    """Wealth after each ledger row, growing only the part not held as margin."""
    days = [row["date"] for row in ledger]
    values = {}
    wealth, held, previous_net = initial, 0, 0
    for i, row in enumerate(ledger):
        growth = marks[days[i]] / marks[days[max(0, i - 1)]]
        wealth += (wealth - held) * (growth - 1) + row["cumulative_net_rub"] - previous_net
        values[row["date"]] = wealth
        held = held_margin(row, trades, automatic)
        previous_net = row["cumulative_net_rub"]
    return values, days


def expected_excess_pp(values, marks, days, initial, share):
    """Two-legged annualization between the split anchor and the last published day."""
    split = EvaluationSplit.from_dates(days, share)
    anchor = split.training[-1] if share else split.training[0]
    opening = values[anchor] if share else initial
    span = (date.fromisoformat(days[-1]) - date.fromisoformat(anchor)).days
    return 100 * (
        (values[days[-1]] / opening) ** (365 / span)
        - (marks[days[-1]] / marks[anchor]) ** (365 / span)
    )


def runtime_excess_pp(proof, marks, days, share):
    """Runtime annualized excess on the evaluation split rebuilt from the ledger days."""
    split = EvaluationSplit.from_dates(days, share)
    return _annualized_returns(proof, marks, split)["annualized_excess_pp"]
