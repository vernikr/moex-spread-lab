"""Calendar-spread universe constants, contract chains, and input terms/FX loaders."""

from __future__ import annotations

import itertools
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.account_risk import (
    DEFAULT_STRESS_VM_BUFFER,
    DEFAULT_UNCERTAINTY_BUFFER,
)
from moex_spread_lab.core.settings import INITIAL_CAPITAL, MIN_DAILY_VOLUME
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.iss.catalog import IssHistoryError, fetch_history

from ..base import ZSCORE_CALENDAR_OPTIONS
from ..calendar_eval import FILLS, strategy_runner

QUARTER_CODES = "HMUZ"
MONTH_CODES = "FGHJKMNQUVXZ"
HISTORY_FIELDS = (
    "TRADEDATE",
    "SECID",
    "OPEN",
    "LOW",
    "HIGH",
    "CLOSE",
    "SETTLEPRICE",
    "VOLUME",
    "NUMTRADES",
)
ASSETS: dict[str, dict[str, Any]] = {
    "SR": {"group": "stock", "convergence": True, "name": "SBRF"},
    "GZ": {"group": "stock", "convergence": True, "name": "GAZR"},
    "LK": {"group": "stock", "name": "LKOH"},
    "RN": {"group": "stock", "name": "ROSN"},
    "SP": {"group": "stock", "name": "SBPR"},
    "MG": {"group": "stock", "name": "MGNT"},
    "YD": {"group": "stock", "name": "YDEX"},
    "AL": {"group": "stock", "name": "ALRS"},
    "TN": {"group": "stock", "name": "TATN"},
    "SG": {"group": "stock", "name": "SNGP"},
    "MM": {"group": "index", "name": "MXI (mini IMOEX)"},
    "MX": {"group": "index", "name": "MIX (IMOEX)"},
    "RI": {"group": "index", "fx": "usd", "name": "RTS"},
    "RB": {"group": "interest", "name": "RGBI"},
    "Si": {"group": "currency", "name": "USD/RUB"},
    "Eu": {"group": "currency", "name": "EUR/RUB"},
    "CR": {"group": "currency", "name": "CNY/RUB"},
    "ED": {"group": "currency", "fx": "usd", "name": "EUR/USD"},
    "UC": {"group": "currency", "fx": "cny", "name": "USD/CNY"},
    "GD": {"group": "commodity", "fx": "usd", "name": "GOLD"},
    "GN": {"group": "commodity", "fx": "usd", "name": "GOLDM"},
    "PT": {"group": "commodity", "fx": "usd", "name": "PLT"},
    "SV": {"group": "commodity", "fx": "usd", "name": "SILV"},
    "BR": {"group": "commodity", "fx": "usd", "monthly": True, "name": "Brent"},
    "NG": {"group": "commodity", "fx": "usd", "monthly": True, "name": "Natural gas"},
    "WT": {"group": "commodity", "fx": "usd", "monthly": True, "name": "WTI"},
}
FX_SOURCE: dict[str, str] = {"usd": "Si", "cny": "CR"}
CROSS_EXPIRY_PAIRS: tuple[dict[str, str], ...] = (
    {
        "pair_id": "MX/MM",
        "near": "MX",
        "far": "MM",
        "family": "cross_contract_same_expiry",
        "name": "MIX vs MXI",
    },
    {
        "pair_id": "GD/GN",
        "near": "GD",
        "far": "GN",
        "family": "cross_contract_same_expiry",
        "name": "GOLD vs GOLDM",
    },
)
SYNTHETIC_TRIANGLE_PAIRS: tuple[dict[str, Any], ...] = (
    {
        "pair_id": "Si+Eu/ED",
        "legs": ("Si", "Eu", "ED"),
        "family": "synthetic_triangle_same_expiry",
        "name": "USD/RUB + EUR/RUB vs EUR/USD",
    },
    {
        "pair_id": "Si+CR/UC",
        "legs": ("Si", "CR", "UC"),
        "family": "synthetic_triangle_same_expiry",
        "name": "USD/RUB + CNY/RUB vs USD/CNY",
    },
)
EXIT_BUFFER_DAYS = 2
HISTORY_FROM = "2022-06-01"
ZSCORE_OPTIONS = ZSCORE_CALENDAR_OPTIONS
STRATEGIES = ("convergence_to_ruonia", "zscore_mean_reversion")
FILL_LABELS = tuple(label for label, _delay, _fill in FILLS)

MIN_H1_CALENDAR_OBSERVATIONS = 22
MIN_H1_NATIVE_SESSION_VOLUME = MIN_DAILY_VOLUME
MIN_H1_PROXY_LEG_SESSION_VOLUME = MIN_DAILY_VOLUME

_calendar_strategy_runner = strategy_runner


def contract_chain(asset: str, first_year: int = 3, last_year: int = 7) -> list[str]:
    monthly = ASSETS.get(asset, {}).get("monthly", False)
    codes, last = (MONTH_CODES, "F") if monthly else (QUARTER_CODES, "H")
    chain = [f"{asset}{code}{year}" for year in range(first_year, last_year + 1) for code in codes]
    return chain[: chain.index(f"{asset}{last}{last_year}") + 1]


def spread_id(near: str, far: str) -> str:
    return near + far


def fetch_inputs(
    cache_dir: Path,
    assets: tuple[str, ...],
    till_date: str,
    *,
    delay_seconds: float = 0.05,
    opener: Any = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """EOD history of every leg and consecutive native spread (plus FX sources), keyed by secid then date."""
    needed = set(assets) | {FX_SOURCE[ASSETS[a]["fx"]] for a in assets if ASSETS[a].get("fx")}
    extra: dict[str, Any] = {"opener": opener} if opener is not None else {}
    secids: list[str] = []
    for asset in sorted(needed):
        chain = contract_chain(asset)
        secids += [*chain, *(spread_id(n, f) for n, f in itertools.pairwise(chain))]

    def load(secid: str) -> tuple[str, dict[str, dict[str, Any]] | None]:
        try:
            source = fetch_history(
                secid,
                HISTORY_FROM,
                till_date,
                cache_dir=cache_dir,
                delay_seconds=delay_seconds,
                fields=HISTORY_FIELDS,
                **extra,
            )
        except IssHistoryError:
            return secid, None
        return secid, {str(row["TRADEDATE"]): row for row in source["rows"]}

    if opener is not None:
        return {secid: rows for secid in secids if (rows := load(secid)[1])}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return {secid: rows for secid, rows in pool.map(load, secids) if rows}


def cached_till_date(cache_dir: Path) -> str | None:
    found = {
        m.group(1)
        for path in cache_dir.glob("*.json")
        if (m := re.search(rf"-{HISTORY_FROM}-(\d{{4}}-\d{{2}}-\d{{2}})-\d+\.json$", path.name))
    }
    return max(found) if found else None


def _offline(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("offline: calendar history is read from the local cache only")


def _ruonia(benchmark_cache: Path) -> tuple[list[str], dict[str, float]]:
    try:
        rows = json.loads(benchmark_cache.read_text(encoding="utf-8"))["rows"]
        rates = {str(row["date"]): float(row["rate"]) / 100 for row in rows}
    except (OSError, ValueError, KeyError) as exc:
        raise W0Error(f"cannot read RUONIA cache: {exc}") from exc
    return sorted(rates), rates


def _is_pos_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0


def _terms(contracts_path: Path, assets: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    try:
        instruments = [
            r
            for r in json.loads(contracts_path.read_text(encoding="utf-8"))["instruments"]
            if r.get("expiry") and r["expiry"] < "2100"
        ]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise W0Error(f"cannot read current contract terms: {exc}") from exc
    terms: dict[str, dict[str, Any]] = {}
    for asset in assets:
        live = sorted(
            (
                r
                for r in instruments
                if str(r["secid"]).startswith(asset) and len(str(r["secid"])) == len(asset) + 2
            ),
            key=lambda r: r["expiry"],
        )
        if not live:
            continue
        row = live[0]
        if not all(
            _is_pos_num(row.get(k))
            for k in ("tick_value", "tick_size", "initial_margin", "settlement")
        ):
            continue
        mult = float(row["tick_value"]) / float(row["tick_size"])
        terms[asset] = {
            "mult": mult,
            "tick_value": row["tick_value"],
            "tick_size": row["tick_size"],
            "margin_rate": float(row["initial_margin"]) / (float(row["settlement"]) * mult),
            "reference_contract": row["secid"],
        }
    return terms


def _fx_factors(histories: dict[str, dict[str, dict[str, Any]]]) -> dict[str, dict[str, float]]:
    """RUB value of a point relative to today, from the front quarterly contract of Si or CR."""
    factors: dict[str, dict[str, float]] = {}
    for kind, asset in FX_SOURCE.items():
        chain = [(max(histories[s]), s) for s in contract_chain(asset) if histories.get(s)]
        if not chain:
            continue
        dates = sorted({d for s in contract_chain(asset) for d in histories.get(s, {})})
        scale = 1000.0 if kind == "usd" else 1.0
        fronts: dict[str, float] = {}
        for day in dates:
            live = [
                s
                for expiry, s in sorted(chain)
                if expiry >= day and day in histories[s] and histories[s][day]["SETTLEPRICE"]
            ]
            if live:
                fronts[day] = float(histories[live[0]][day]["SETTLEPRICE"]) / scale
        if fronts:
            latest = fronts[max(fronts)]
            factors[kind] = {day: value / latest for day, value in fronts.items()}
    return factors


def _positive_spread_volume_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row for row in rows if row.get("min_volume") is not None and float(row["min_volume"]) > 0
    ]


def _stress_ok(margin: float, quantity: int = 1, initial_capital: float = INITIAL_CAPITAL) -> bool:
    buffers = (DEFAULT_STRESS_VM_BUFFER + DEFAULT_UNCERTAINTY_BUFFER) * quantity
    return 3 * margin * quantity + buffers <= initial_capital


def _zscore_options(asset: str) -> dict[str, Any]:
    return dict(ZSCORE_OPTIONS["monthly" if ASSETS[asset].get("monthly") else "quarterly"])
