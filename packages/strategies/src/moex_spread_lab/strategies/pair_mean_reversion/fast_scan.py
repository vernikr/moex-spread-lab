"""Fastcore intent scan and rolling Z-score helpers for ordinary pair mean reversion."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, cast

from ..base import population_rolling_stats as _rolling_stats
from .series_builder import AnalyticsError
from .series_builder import round_value as _round

_fastcore_loader = import_module("moex_spread_lab.core._fastcore_loader")
load_fastcore = cast(Callable[[], Any], _fastcore_loader.load_fastcore)

_ACTION_NAMES = (
    "hold_flat",
    "hold_position",
    "exit",
    "enter_long_spread",
    "enter_short_spread",
    "forced_exit_end_of_sample",
)


def compute_rolling_z(
    spread_ticks: list[float],
    lookback: int,
    decision_delay_bars: int,
) -> list[float | None]:
    """Compute exact population z-scores matching run_mean_reversion_baseline."""
    fc = load_fastcore()
    if fc is not None and lookback > 0 and decision_delay_bars >= 0:
        return fc.compute_rolling_z(spread_ticks, lookback, decision_delay_bars)
    n = len(spread_ticks)
    result: list[float | None] = [None] * n
    start = lookback + decision_delay_bars
    for index in range(start, n):
        signal_index = index - decision_delay_bars
        window = spread_ticks[signal_index - lookback : signal_index]
        mean, standard_deviation = _rolling_stats(window)
        deviation = spread_ticks[signal_index] - mean
        result[index] = deviation / standard_deviation if standard_deviation > 0 else 0.0
    return result


@dataclass
class BaselineParams:
    initial_capital: float = 30000.0
    lookback: int = 20
    entry_z: float = 2.0
    exit_z: float = 0.5
    quantity: int = 1
    decision_delay_bars: int = 1
    analysis_role: str = "primary_baseline"
    cost_profile: dict[str, Any] | None = None
    fill: str = "close"
    cash_in_lqdt: bool = False
    funding_swaprates: dict[str, float] = field(default_factory=dict)
    sizing_intents: bool = False
    metrics_only: bool = False
    precomputed_z: list[float | None] | None = None


def parse_baseline_params(options: dict[str, Any]) -> BaselineParams:
    allowed = set(BaselineParams.__dataclass_fields__)
    unknown = set(options) - allowed
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {sorted(unknown)}")
    raw_funding = options.get("funding_swaprates")
    params = BaselineParams(
        initial_capital=float(options.get("initial_capital", 30000.0)),
        lookback=int(options.get("lookback", 20)),
        entry_z=float(options.get("entry_z", 2.0)),
        exit_z=float(options.get("exit_z", 0.5)),
        quantity=int(options.get("quantity", 1)),
        decision_delay_bars=int(options.get("decision_delay_bars", 1)),
        analysis_role=str(options.get("analysis_role", "primary_baseline")),
        cost_profile=options.get("cost_profile"),
        fill=str(options.get("fill", "close")),
        cash_in_lqdt=bool(options.get("cash_in_lqdt", False)),
        funding_swaprates=dict(raw_funding) if raw_funding else {},
        sizing_intents=bool(options.get("sizing_intents", False)),
        metrics_only=bool(options.get("metrics_only", False)),
        precomputed_z=options.get("precomputed_z"),
    )
    if (
        params.initial_capital <= 0
        or params.lookback < 2
        or params.entry_z <= 0
        or params.exit_z < 0
        or params.quantity <= 0
        or params.decision_delay_bars < 0
        or (
            params.decision_delay_bars == 0
            and (params.analysis_role != "diagnostic_only" or params.fill != "close")
        )
        or params.analysis_role not in {"primary_baseline", "diagnostic_only"}
        or params.fill not in {"close", "open"}
        or (
            params.sizing_intents
            and (params.analysis_role != "diagnostic_only" or params.quantity != 1)
        )
    ):
        raise AnalyticsError("invalid baseline parameters")
    return params


def _fills(row: dict[str, Any], use_open: bool) -> tuple[Any, Any]:
    return (
        (row["near_open"], row["far_open"]) if use_open else (row["near_close"], row["far_close"])
    )


def _fast_trade_row(
    rows: list[dict[str, Any]],
    params: BaselineParams,
    near_multiplier: float,
    far_multiplier: float,
    trade: tuple[int, int, int, float, bool],
) -> dict[str, Any]:
    """One journal row of the C scan: settlement is always by close, fills follow the mode."""
    e_idx, x_idx, pos_val, sig_z, forced = trade
    entry_row, exit_row = rows[e_idx], rows[x_idx]
    e_near, e_far = _fills(entry_row, params.fill == "open")
    x_near, x_far = _fills(exit_row, params.fill == "open" and not forced)
    return {
        "entry_date": entry_row.get("timestamp", entry_row["date"]),
        "exit_date": exit_row.get("timestamp", exit_row["date"]),
        "direction": "long_near_short_far" if pos_val > 0 else "short_near_long_far",
        "quantity": params.quantity,
        "entry_near_fill": e_near,
        "entry_far_fill": e_far,
        "exit_near_fill": x_near,
        "exit_far_fill": x_far,
        "entry_spread_fill": _round(e_near * near_multiplier - e_far * far_multiplier),
        "exit_spread_fill": _round(x_near * near_multiplier - x_far * far_multiplier),
        "entry_signal_z": _round(sig_z),
        "exit_reason": "end_of_sample" if forced else "mean_reversion_exit",
    }


def _fast_daily_row(
    row: dict[str, Any],
    act: int,
    pos_val: int,
    swaprates: Mapping[str, float],
    lot_q: float,
) -> dict[str, Any]:
    day = row["date"]
    swaprate = swaprates.get(day) if pos_val else None
    return {
        "date": day,
        "action": _ACTION_NAMES[act],
        "position": 0 if act == 5 else pos_val,
        "funding_rub": _round(-pos_val * float(swaprate) * lot_q) if swaprate is not None else 0.0,
    }


def try_fast_intents_scan(
    rows: list[dict[str, Any]],
    params: BaselineParams,
    near_multiplier: float,
    far_multiplier: float,
    lot_size: float,
) -> dict[str, Any] | None:
    if not (params.metrics_only and params.sizing_intents and params.precomputed_z is not None):
        return None
    fc = load_fastcore()
    if fc is None:
        return None
    trade_tuples, action_pairs = fc.mean_reversion_scan(
        params.precomputed_z,
        len(rows),
        params.lookback,
        params.decision_delay_bars,
        params.entry_z,
        params.exit_z,
        bool(params.funding_swaprates),
    )
    trades = [
        _fast_trade_row(
            rows, params, near_multiplier, far_multiplier, (e_idx, x_idx, pos_val, sig_z, forced)
        )
        for e_idx, x_idx, pos_val, sig_z, forced in cast(
            list[tuple[int, int, int, float, bool]], trade_tuples
        )
    ]
    daily: list[dict[str, Any]] = []
    if params.funding_swaprates and action_pairs is not None:
        lot_q = lot_size * params.quantity
        daily = [
            _fast_daily_row(row, act, pos_val, params.funding_swaprates, lot_q)
            for row, (act, pos_val) in zip(
                rows, cast(list[tuple[int, int]], action_pairs), strict=True
            )
        ]
    return {"trades": trades, "daily": daily}
