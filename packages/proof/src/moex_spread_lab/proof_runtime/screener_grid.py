"""Bounded search grid and immediate axis-neighbor construction."""

from itertools import product
from typing import Any

from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerError


def grid(strategy: str, seed: dict[str, Any]) -> tuple[list[str], list[tuple[Any, ...]]]:
    lookbacks = sorted(
        {value for delta in (-5, 0, 5) if 5 <= (value := seed["lookback"] + delta) <= 80}
    )
    if strategy == "funding_only":
        lookbacks = sorted(
            {
                value
                for delta in (-10, -5, 0, 5, 10)
                if 5 <= (value := seed["lookback"] + delta) <= 80
            }
        )
        return ["lookback"], [(value,) for value in lookbacks]
    entries = sorted(
        {
            round(value, 3)
            for delta in (-0.5, 0, 0.5)
            if 0.5 <= (value := seed["entry_z"] + delta) <= 5
        }
    )
    exits = sorted(
        {
            round(value, 3)
            for delta in (-0.25, 0, 0.25)
            if 0 <= (value := seed["exit_z"] + delta) < min(entries)
        }
    )
    if len(lookbacks) < 3 or len(entries) < 3 or len(exits) < 3:
        raise ScreenerError(
            "choose seed parameters away from the boundaries to search a complete neighborhood"
        )
    return ["lookback", "entry_z", "exit_z"], list(product(lookbacks, entries, exits))


def neighbors(point: tuple[Any, ...], points: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    axes = [sorted({item[index] for item in points}) for index in range(len(point))]
    result: list[tuple[Any, ...]] = []
    for other in points:
        changed = [index for index in range(len(point)) if point[index] != other[index]]
        if len(changed) != 1:
            continue
        index = changed[0]
        if abs(axes[index].index(point[index]) - axes[index].index(other[index])) == 1:
            result.append(other)
    return result
