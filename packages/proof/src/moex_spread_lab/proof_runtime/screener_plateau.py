"""Select stable search neighborhoods without changing trial order."""

from collections.abc import Callable
from typing import Any


def select_plateau(
    strategy: str,
    axes: list[str],
    points: list[tuple[Any, ...]],
    trials: list[dict[str, Any]],
    neighbors_for: Callable[[tuple[Any, ...], list[tuple[Any, ...]]], list[tuple[Any, ...]]],
    plateau_ratio: float,
) -> tuple[
    dict[tuple[Any, ...], dict[str, Any]],
    tuple[float, tuple[Any, ...], list[tuple[Any, ...]]] | None,
]:
    by_point = {tuple(trial["parameters"][axis] for axis in axes): trial for trial in trials}
    plateaus: list[tuple[float, tuple[Any, ...], list[tuple[Any, ...]]]] = []
    for point, trial in by_point.items():
        if trial["status"] != "evaluated" or trial["train_min_excess_pp"] <= 0:
            continue
        neighbors = neighbors_for(point, points)
        stable = [
            other
            for other in neighbors
            if by_point[other]["status"] == "evaluated"
            and by_point[other]["train_min_excess_pp"]
            >= plateau_ratio * trial["train_min_excess_pp"]
            and by_point[other]["train_min_excess_pp"] > 0
        ]
        trial["stable_neighbor_count"] = len(stable)
        required = 2 if strategy == "funding_only" else 3
        if len(stable) >= required:
            trial["plateau"] = True
            plateaus.append(
                (
                    min(by_point[other]["train_min_excess_pp"] for other in (*stable, point)),
                    point,
                    stable,
                )
            )
    plateaus.sort(reverse=True)
    chosen = plateaus[0] if plateaus else None
    return by_point, chosen
