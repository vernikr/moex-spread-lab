"""Marked daily/intraday ledger simulation and compact series helpers for calendar proofs."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from moex_spread_lab.core._fastcore_loader import load_fastcore

from ..calendar_ruonia.engine import QUARTER_DAYS


def rel_pct(value: Any, base: Any) -> float | None:
    """Spread as % of the far price. Pure same-row ratio — strictly causal."""
    try:
        numerator = float(value)
        denominator = float(base)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator == 0.0:
        return None
    return round(numerator / denominator * 100, 6)


def clean_payload(value: Any) -> Any:
    fc = load_fastcore()
    if fc is not None:
        return fc.clean_obj(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: clean_payload(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_payload(v) for v in value]
    return value


def elapsed_days(start: str, end: str) -> float:
    if "T" in start or "T" in end:
        left = datetime.fromisoformat(str(start).replace("Z", ""))
        right = datetime.fromisoformat(str(end).replace("Z", ""))
        return round((right - left).total_seconds() / 86400, 4)
    return float((date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days)


@dataclass
class _LedgerSim:
    quantity: int
    by_entry: dict[str, dict[str, Any]]
    by_exit: dict[str, dict[str, Any]]
    marked: dict[int, float]
    position: dict[str, Any] | None = None
    cumulative: float = 0.0
    ledger: list[dict[str, Any]] = field(default_factory=list)


def _step_open_position(
    sim: _LedgerSim, row: dict[str, Any], day: str, point: float, vol_ok: bool, ext_ok: bool
) -> tuple[float, float]:
    if sim.position is None:
        return 0.0, 0.0
    exiting = sim.by_exit.get(day)
    forced_end = exiting is not None and exiting["reason"] == "end_of_window"
    if exiting is not None and (ext_ok or forced_end):
        end = float(exiting["exit"]["spread_fill"])
        move = sim.position["sign"] * (end - sim.position["mark"]) * point
        sim.marked[sim.position["number"]] += move
        cost = sum(exiting["exit"]["costs"].values())
        sim.position = None
        return move, cost
    if vol_ok:
        sp_px = float(row["spread_price"])
        move = sim.position["sign"] * (sp_px - sim.position["mark"]) * point
        sim.marked[sim.position["number"]] += move
        sim.position["mark"] = sp_px
        return move, 0.0
    return 0.0, 0.0


def _step_entry_position(
    sim: _LedgerSim, row: dict[str, Any], day: str, point: float, vol_ok: bool, ent_ok: bool
) -> tuple[float, float]:
    entering = sim.by_entry.get(day) if ent_ok else None
    if not entering:
        return 0.0, 0.0
    sign = 1 if entering["side"] == "long_spread" else -1
    cost = sum(entering["entry"]["costs"].values())
    ent_fill = float(entering["entry"]["spread_fill"])
    if vol_ok:
        sp_px = float(row["spread_price"])
        move = sign * (sp_px - ent_fill) * point
        sim.marked[entering["number"]] += move
        mark = sp_px
    else:
        move, mark = 0.0, ent_fill
    sim.position = {"sign": sign, "mark": mark, "number": int(entering["number"])}
    return move, cost


def simulate_marked_ledger(
    rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    quantity: int,
    *,
    compact: bool,
) -> tuple[list[dict[str, Any]], dict[int, float], float]:
    sim = _LedgerSim(
        quantity=quantity,
        by_entry={t["entry"]["date"]: t for t in trades},
        by_exit={t["exit"]["date"]: t for t in trades},
        marked={int(t["number"]): 0.0 for t in trades},
    )
    for row in rows:
        day = row["date"]
        point = float(row["mult"]) * quantity
        vol_ok = bool(
            row.get("volume_eligible", row.get("min_volume") is not None and row["min_volume"] > 0)
        )
        ent_ok = bool(row.get("entry_eligible", vol_ok))
        ext_ok = bool(row.get("exit_eligible", vol_ok))
        m1, c1 = _step_open_position(sim, row, day, point, vol_ok, ext_ok)
        m2, c2 = _step_entry_position(sim, row, day, point, vol_ok, ent_ok)
        daily, cost = m1 + m2, c1 + c2
        sim.cumulative += daily - cost
        sim.ledger.append(
            {"date": day, "cumulative_net_rub": round(sim.cumulative, 2)}
            if compact
            else {
                "date": day,
                "position": sim.position["sign"] if sim.position else 0,
                "mark_pnl_rub": round(daily, 2),
                "costs_rub": round(cost, 2),
                "cumulative_net_rub": round(sim.cumulative, 2),
            }
        )
    return sim.ledger, sim.marked, sim.cumulative


def compact_series(
    rows: list[dict[str, Any]], series_cache: dict[Any, Any] | None, series_key: Any
) -> list[dict[str, Any]]:
    if series_cache is not None and series_key in series_cache:
        return series_cache[series_key]
    compact = [
        {
            "date": r["date"],
            "session_date": r.get("session_date", r["date"][:10]),
            "benchmark_mark_date": r.get("benchmark_mark_date"),
            "near": r["near_price"],
            "far": r["far_price"],
            "near_open": r["near_open_price"],
            "far_open": r["far_open_price"],
            "spread": r["spread_price"],
            "volume_eligible": r.get("volume_eligible", r["min_volume"] > 0),
            "entry_eligible": r.get(
                "entry_eligible", r.get("volume_eligible", r["min_volume"] > 0)
            ),
            "exit_eligible": r.get("exit_eligible", r.get("volume_eligible", r["min_volume"] > 0)),
            "point_value_rub": r["mult"],
        }
        for r in rows
    ]
    if series_cache is not None:
        series_cache[series_key] = compact
    return compact


def full_event_dict(row: dict[str, Any], fee: dict[str, float], is_open: bool) -> dict[str, Any]:
    sp_fill = row["spread_open_price"] if is_open else row["spread_price"]
    far_px = row["far_open_price"] if is_open else row["far_price"]
    near_px = row["near_open_price"] if is_open else row["near_price"]
    return {
        "date": row["date"],
        "spread_fill": sp_fill,
        "spread_fill_rel": rel_pct(sp_fill, far_px),
        "near_price": near_px,
        "far_price": far_px,
        "spread_volume": row.get("spread_volume", row["min_volume"]),
        "near_volume": row["near_volume"],
        "far_volume": row["far_volume"],
        "costs": fee,
    }


def full_series_row(r: dict[str, Any], tf: str, quantity: int) -> dict[str, Any]:
    vol_ok = r.get("volume_eligible", r["min_volume"] > 0)
    ruonia = r.get("ruonia_rate")
    return {
        "date": r["date"],
        "timestamp": r.get("timestamp", r["date"]),
        "session_date": r.get("session_date", r["date"][:10]),
        "benchmark_mark_date": r.get("benchmark_mark_date"),
        "timeframe": tf,
        "near": r["near_price"],
        "far": r["far_price"],
        "near_open": r["near_open_price"],
        "far_open": r["far_open_price"],
        "spread": r["spread_price"],
        "spread_open": r["spread_open_price"],
        "spread_rel": rel_pct(r["spread_price"], r["far_price"]),
        "spread_open_rel": rel_pct(r["spread_open_price"], r["far_open_price"]),
        "spread_last_trade_rel": rel_pct(r["spread_last_trade"], r["far_price"]),
        "spread_last_trade": r["spread_last_trade"],
        "spread_volume": r.get("spread_volume", r["min_volume"]),
        "volume_eligible": vol_ok,
        "signal_eligible": r.get("signal_eligible", vol_ok),
        "entry_eligible": r.get("entry_eligible", vol_ok),
        "exit_eligible": r.get("exit_eligible", vol_ok),
        "spread_trades": r["spread_trades"],
        "near_volume": r["near_volume"],
        "far_volume": r["far_volume"],
        "days_to_near_expiry": r["days_to_near_expiry"],
        "dev_pp": r["dev_pp"],
        "ruonia_rate_pct": float(ruonia) * 100 if ruonia is not None else None,
        "ruonia_rate_date": r.get("ruonia_rate_date"),
        "rate_price_per_pp": r["near_price"] * QUARTER_DAYS / 365 / 100,
        "margin_rub": round(r["margin"] * quantity, 2),
        "point_value_rub": r["mult"],
    }


def sizing_intents_trades(account: dict[str, Any], is_open: bool) -> list[dict[str, Any]]:
    return [
        {
            "number": number,
            "side": "short_spread" if trade["direction"] < 0 else "long_spread",
            "signal": trade["signal"],
            "entry": {
                "date": trade["entry_row"]["date"],
                "spread_fill": trade["entry_row"]["spread_open_price"]
                if is_open
                else trade["entry_row"]["spread_price"],
                "near_price": trade["entry_row"]["near_open_price"]
                if is_open
                else trade["entry_row"]["near_price"],
                "far_price": trade["entry_row"]["far_open_price"]
                if is_open
                else trade["entry_row"]["far_price"],
            },
            "exit": {
                "date": trade["exit_row"]["date"],
                "spread_fill": trade["exit_row"]["spread_open_price"]
                if is_open
                else trade["exit_row"]["spread_price"],
                "near_price": trade["exit_row"]["near_open_price"]
                if is_open
                else trade["exit_row"]["near_price"],
                "far_price": trade["exit_row"]["far_open_price"]
                if is_open
                else trade["exit_row"]["far_price"],
            },
            "reason": trade["reason"],
        }
        for number, trade in enumerate(account["trade_list"], start=1)
    ]
