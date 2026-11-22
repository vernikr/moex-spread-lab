"""Pure helpers shared by the proof engines, the service adapters and the facade.

Everything here is stateless: row lookups, benchmark marks, contract terms and
fill/trade/ledger row construction. Keeping these free of `ProofService` state
lets the engines and adapters stay small enough to read in one go.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import W0Error, _HashedJSONCache, read_hashed_json

PROOF_SCHEMA = "execution-proof.v1"
FORCED_EXIT_REASONS = frozenset(
    {"end_of_sample", "end_of_window", "open_at_data_end_no_executable_exit"}
)

_BENCHMARK_MARKS_CACHE = _HashedJSONCache(32)
_INSTRUMENTS_CACHE = _HashedJSONCache(32)
_SHA256_CACHE = _HashedJSONCache(512)


def clean(value: Any) -> Any:
    """Replace non-finite floats with None; delegate to the C99 cleaner when built."""
    from moex_spread_lab.core._fastcore_loader import load_fastcore

    fc = load_fastcore()
    if fc is not None:
        return fc.clean_obj(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value


def sha256_bytes(path: Path) -> str | None:
    """Content hash of a file, memoised on (mtime, size); None when unreadable."""
    try:
        resolved = path.resolve()
        stat = resolved.stat()
    except OSError:
        return None
    key = (str(resolved), stat.st_mtime_ns, stat.st_size)
    cached = _SHA256_CACHE.get(key)
    if cached is not None:
        return cached[1]
    try:
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError:
        return None
    _SHA256_CACHE.put(key, (None, digest))
    return digest


def _benchmark_marks_from(payload: dict[str, Any]) -> tuple[list[str], dict[str, float]]:
    """Sorted date axis and price map from the lineage file's `rows`."""
    rows = [
        (row["date"], float(row["price"]))
        for row in payload.get("rows", [])
        if row.get("date") and row.get("price") is not None
    ]
    rows.sort(key=lambda item: item[0])
    return [day for day, _ in rows], dict(rows)


def benchmark_marks(path: Path) -> tuple[list[str], dict[str, float]]:
    """LQDT benchmark date axis and mark map, memoised on file identity."""
    resolved = path.resolve()
    try:
        stat = resolved.stat()
    except OSError:
        return _benchmark_marks_from(json.loads(path.read_text(encoding="utf-8")))
    key = (str(resolved), stat.st_mtime_ns, stat.st_size)
    cached = _BENCHMARK_MARKS_CACHE.get(key)
    if cached is not None:
        return cached[0]
    payload, _ = read_hashed_json(resolved, error=W0Error, label="LQDT benchmark")
    result = _benchmark_marks_from(payload)
    _BENCHMARK_MARKS_CACHE.put(key, (result, ""))
    return result


def mark(dates: list[str], prices: dict[str, float], day: str) -> float:
    """Last available benchmark mark at or before `day`."""
    index = max(0, bisect_right(dates, day) - 1)
    return prices[dates[index]]


def positive_pair_volume(row: dict[str, Any]) -> bool:
    return all(
        value is not None and float(value) > 0
        for value in (row.get("near_volume"), row.get("far_volume"))
    )


def events(trades: list[dict[str, Any]]) -> dict[str, str]:
    """Journal event labels keyed by date; same-day entries and exits are joined."""
    labels: dict[str, list[str]] = {}
    for trade in trades:
        labels.setdefault(trade["entry"]["date"], []).append(f"ENTRY #{trade['number']}")
        labels.setdefault(trade["exit"]["date"], []).append(f"EXIT #{trade['number']}")
    return {day: " + ".join(names) for day, names in labels.items()}


def result_label(net_rub: float) -> str:
    if net_rub > 0:
        return "win"
    if net_rub < 0:
        return "loss"
    return "flat"


def rel_pct(value: Any, base: Any) -> float | None:
    """Spread as %% of the far leg. Pure same-row ratio — strictly causal, no look-ahead."""
    try:
        numerator = float(value)
        denominator = float(base)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator == 0.0:
        return None
    return round(numerator / denominator * 100, 6)


def load_instruments(contracts_path: Path) -> dict[str, dict[str, Any]]:
    """Current and historical contract terms keyed by secid, memoised on file identity."""
    try:
        resolved = contracts_path.resolve()
        stat = resolved.stat()
        historical_path = resolved.parent / "historical-contracts.json"
        h_stat = historical_path.stat() if historical_path.is_file() else None
    except OSError:
        return {}
    key = (
        str(resolved),
        stat.st_mtime_ns,
        stat.st_size,
        h_stat.st_mtime_ns if h_stat else None,
        h_stat.st_size if h_stat else None,
    )
    cached = _INSTRUMENTS_CACHE.get(key)
    if cached is not None:
        return cached[0]
    try:
        main_payload, _ = read_hashed_json(resolved, error=W0Error, label="contracts")
        rows = list(main_payload["instruments"])
        if h_stat is not None:
            hist_payload, _ = read_hashed_json(
                historical_path, error=W0Error, label="historical contracts"
            )
            rows.extend(hist_payload.get("instruments", []))
    except (W0Error, OSError, ValueError, KeyError, TypeError):
        return {}
    mapping = {row["secid"]: row for row in rows if isinstance(row, dict) and row.get("secid")}
    _INSTRUMENTS_CACHE.put(key, (mapping, ""))
    return mapping


def load_contracts_for_side_cost(
    contracts_path: Path, near: str, far: str
) -> dict[str, dict[str, Any]]:
    """Uncached contract rows for the two legs, including historical specifications."""
    payload = json.loads(contracts_path.read_text(encoding="utf-8"))
    rows = list(payload.get("instruments", []))
    historical_path = contracts_path.parent / "historical-contracts.json"
    if historical_path.is_file():
        historical = json.loads(historical_path.read_text(encoding="utf-8"))
        rows.extend(historical.get("instruments", []))
    contracts: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("secid") in (near, far):
            contracts[row["secid"]] = row
    return contracts


def point_value(terms: dict[str, Any], fallback: float = 1.0) -> float:
    try:
        tick_size = float(terms["tick_size"])
        if tick_size == 0:
            return fallback
        return float(terms["tick_value"]) / tick_size
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return fallback


def legs_label(side: str) -> str:
    return "buy near / sell far" if side == "long_spread" else "sell near / buy far"


def days_held(entry_date: str, exit_date: str) -> int | float | None:
    try:
        if "T" in entry_date or "T" in exit_date:
            start = datetime.fromisoformat(entry_date.replace("Z", ""))
            end = datetime.fromisoformat(exit_date.replace("Z", ""))
            return round((end - start).total_seconds() / 86400, 4)
        return (date.fromisoformat(exit_date[:10]) - date.fromisoformat(entry_date[:10])).days
    except ValueError:
        return None


def observed_min_leg_volume(series_rows: list[dict[str, Any]]) -> float | None:
    """Smallest observed min-leg volume; rows with a missing or zero leg are skipped."""
    volumes: list[float] = []
    for row in series_rows:
        near_volume = row.get("near_session_volume", row.get("near_volume"))
        far_volume = row.get("far_session_volume", row.get("far_volume"))
        if near_volume is None or far_volume is None:
            continue
        if float(near_volume) > 0 and float(far_volume) > 0:
            volumes.append(min(float(near_volume), float(far_volume)))
    return min(volumes) if volumes else None


def fill_block(
    day: str,
    spread_fill: float,
    near_price: float,
    far_price: float,
    rows_by_date: dict[str, dict[str, Any]],
    costs: dict[str, float] | None,
    far_value: float | None = None,
) -> dict[str, Any]:
    row = rows_by_date.get(day, {})
    return {
        "date": day,
        "benchmark_mark_date": row.get("benchmark_mark_date"),
        "spread_fill": spread_fill,
        "spread_fill_rel": rel_pct(spread_fill, far_value if far_value is not None else far_price),
        "near_price": near_price,
        "far_price": far_price,
        "near_volume": row.get("near_volume") if "near_volume" in row else row.get("volume"),
        "far_volume": row.get("far_volume"),
        "spread_volume": row.get("spread_volume"),
        "margin_rub": None,
        "costs": {"exchange_clearing": 0.0, "broker": 0.0, "slippage": 0.0, **(costs or {})},
    }


@dataclass(frozen=True, slots=True)
class TradeSpec:
    """Everything one trade row needs besides the benchmark mark axis."""

    number: int
    side: str
    quantity: int
    signal: dict[str, Any]
    entry: dict[str, Any]
    exit: dict[str, Any]
    exit_reason: str
    margin_pair: float
    gross_rub: float
    costs_rub: float
    net_rub: float
    open_at_end: bool | None = None
    funding_rub: float | None = None


def trade_block(
    spec: TradeSpec,
    benchmark_dates: list[str],
    benchmark_prices: dict[str, float],
) -> dict[str, Any]:
    """One trade row; the LQDT hurdle uses benchmark marks around the holding period."""
    entry, exit_ = spec.entry, spec.exit
    open_at_end = (
        spec.open_at_end
        if spec.open_at_end is not None
        else spec.exit_reason in FORCED_EXIT_REASONS
    )
    entry_mark = mark(
        benchmark_dates, benchmark_prices, entry.get("benchmark_mark_date") or entry["date"][:10]
    )
    exit_mark = mark(
        benchmark_dates, benchmark_prices, exit_.get("benchmark_mark_date") or exit_["date"][:10]
    )
    hurdle = spec.margin_pair * (exit_mark / entry_mark - 1)
    return {
        "number": spec.number,
        "side": spec.side,
        "legs": legs_label(spec.side),
        "direction_detail": "long_near_short_far"
        if spec.side == "long_spread"
        else "short_near_long_far",
        "quantity": spec.quantity,
        "signal": spec.signal,
        "entry": entry,
        "exit": exit_,
        "days_held": days_held(entry["date"], exit_["date"]),
        "exit_reason": spec.exit_reason,
        "open_at_end": open_at_end,
        "gross_pts": None,
        "gross_rub": round(spec.gross_rub, 2),
        "funding_rub": round(spec.funding_rub, 2) if spec.funding_rub is not None else None,
        "costs_rub": round(spec.costs_rub, 2),
        "net_rub": round(spec.net_rub, 2),
        "margin_rub": round(spec.margin_pair, 2),
        "lqdt_on_margin_rub": round(hurdle, 2),
        "excess_rub": round(spec.net_rub - hurdle, 2),
        "result": result_label(spec.net_rub),
    }
