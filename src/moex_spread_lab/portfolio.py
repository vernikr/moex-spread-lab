"""Single-account wealth path: free cash is held in LQDT, margin stays in cash and earns nothing (A-11)."""

from __future__ import annotations


def lqdt_overlay_values(
    capital: float,
    cumulative_pnl: list[float],
    margins: list[float],
    benchmark_prices: list[float],
) -> tuple[list[float], int]:
    """Return wealth per row and the number of rows where free cash is negative.

    `cumulative_pnl` is net of costs and includes variation margin; `margins` is the cash
    reserved at the end of each row. Free cash compounds with the LQDT return of each row;
    losses and margin are funded from it, gains and released margin return to it. Margin
    changes are applied at row end, so margin earns the LQDT return of the row it is posted.
    """
    free = capital
    values: list[float] = []
    shortfalls = 0
    for index, pnl in enumerate(cumulative_pnl):
        growth = benchmark_prices[index] / benchmark_prices[index - 1] if index else 1.0
        previous_pnl = cumulative_pnl[index - 1] if index else 0.0
        previous_margin = margins[index - 1] if index else 0.0
        free = free * growth + (pnl - previous_pnl) - (margins[index] - previous_margin)
        shortfalls += free < 0
        values.append(free + margins[index])
    return values, shortfalls
