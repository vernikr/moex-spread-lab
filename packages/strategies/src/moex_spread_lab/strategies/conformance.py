"""Reusable offline strategy conformance checks for plugin authors.

This is a test helper, not runtime certification: a finite fixture cannot prove
causality for every possible dataset or verify broker/exchange assumptions.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from moex_spread_lab.core.contracts import StrategyPlugin, WindowSnapshot

_FORCED = frozenset({"end_of_sample", "end_of_window", "open_at_data_end_no_executable_exit"})


def assert_causal_conformance(
    plugin: StrategyPlugin,
    window: WindowSnapshot,
    chart_series: Sequence[dict[str, Any]],
    params: Mapping[str, Any],
    *,
    cutoffs: Sequence[int],
    require_trade: bool = True,
) -> None:
    """Check determinism and causal truncation on a plugin-owned fixture.

    The two series must be aligned observation-by-observation. At each cutoff,
    compare chart bands and all closed intents strictly before the prefix's
    final observation. Forced end marks aren't treated as executable exits.
    """
    if len(window.rows) != len(chart_series):
        raise AssertionError("window and chart fixture lengths differ")
    if not cutoffs or any(type(n) is not int or n < 2 or n >= len(chart_series) for n in cutoffs):
        raise AssertionError("cutoffs must be inside the fixture and contain at least two rows")
    row_dates = [str(r["date"]) for r in window.rows]
    chart_dates = [str(r["date"]) for r in chart_series]
    if row_dates != chart_dates:
        raise AssertionError("window and chart fixture dates differ")
    if not plugin.supported_fills:
        raise AssertionError("strategy has no supported fills")

    def evaluate(snapshot: WindowSnapshot, fill: str):
        result = plugin.evaluate_intents(deepcopy(snapshot), deepcopy(dict(params)), fill)
        if result.strategy_id != plugin.strategy_id or result.fill != fill:
            raise AssertionError("strategy returned inconsistent identity or fill")
        return result

    def bands(rows: Sequence[dict[str, Any]], fill: str):
        result = plugin.compute_entry_bands(deepcopy(list(rows)), deepcopy(dict(params)), fill)
        if len(result) != len(rows):
            raise AssertionError("entry bands must align with every chart row")
        if any(str(point.date) != str(row["date"]) for point, row in zip(result, rows, strict=True)):
            raise AssertionError("entry band dates do not align with the chart")
        return result

    def closed_before(intents, end):
        return [
            intent for intent in intents
            if str(intent["exit_date"]) < end
            and intent.get("reason") not in _FORCED and not intent.get("open_at_end")
        ]

    for fill in plugin.supported_fills:
        full = evaluate(window, fill)
        if require_trade and not full.intents:
            raise AssertionError(f"fixture must exercise at least one trade for {fill}")
        if full != evaluate(window, fill):
            raise AssertionError(f"nondeterministic intents for {fill}")
        full_bands = bands(chart_series, fill)
        if full_bands != bands(chart_series, fill):
            raise AssertionError(f"nondeterministic bands for {fill}")
        for cutoff in cutoffs:
            prefix = evaluate(window.truncated(cutoff), fill)
            if bands(chart_series[:cutoff], fill) != full_bands[:cutoff]:
                raise AssertionError(f"future-dependent bands for {fill} at {cutoff}")
            end = chart_dates[cutoff - 1]
            if closed_before(prefix.intents, end) != closed_before(full.intents, end):
                raise AssertionError(f"future-dependent closed intents for {fill} at {cutoff}")
