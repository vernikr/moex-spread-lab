"""Native MOEX calendar-spread study over front windows, priced from official leg settlements."""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .account_risk import DEFAULT_STRESS_VM_BUFFER, DEFAULT_UNCERTAINTY_BUFFER
from .carry import CarryError, fetch_history
from .costs import tariff_profile
from .s3_reference import evaluate_variants, read_benchmark
from .screener_app import INITIAL_CAPITAL, MIN_DAILY_VOLUME
from .w0 import W0Error

QUARTER_CODES = "HMUZ"
HISTORY_FIELDS = ("TRADEDATE", "SECID", "OPEN", "LOW", "HIGH", "CLOSE", "SETTLEPRICE", "VOLUME", "NUMTRADES")
ASSETS = {"SR": "stock", "GZ": "stock", "Si": "currency", "CR": "currency"}
CURRENT_LEG = {"SR": "SRZ6", "GZ": "GZZ6", "Si": "SiZ6", "CR": "CRZ6"}
EXIT_BUFFER_DAYS = 2
QUARTER_DAYS = 91
HISTORY_FROM = "2022-06-01"
SBER_EX_DATES = ((date(2023, 5, 9), 25.0), (date(2024, 7, 9), 33.3), (date(2025, 7, 16), 34.84), (date(2026, 7, 18), 37.64))
DIVIDEND_SHARES_PER_CONTRACT = 100


def contract_chain(asset: str, first_year: int = 3, last_year: int = 7) -> list[str]:
    chain = [f"{asset}{code}{year}" for year in range(first_year, last_year + 1) for code in QUARTER_CODES]
    return chain[:chain.index(f"{asset}H{last_year}") + 1]


def spread_id(near: str, far: str) -> str:
    return near + far


def fetch_inputs(cache_dir: Path, assets: tuple[str, ...], till_date: str, *, delay_seconds: float = 0.05) -> dict[str, dict[str, dict[str, Any]]]:
    """EOD history of every quarterly leg and every consecutive native spread, keyed by secid then date."""
    histories: dict[str, dict[str, dict[str, Any]]] = {}
    for asset in assets:
        chain = contract_chain(asset)
        for secid in (*chain, *(spread_id(n, f) for n, f in zip(chain, chain[1:]))):
            try:
                source = fetch_history(secid, HISTORY_FROM, till_date, cache_dir=cache_dir, delay_seconds=delay_seconds, fields=HISTORY_FIELDS)
            except CarryError:
                continue
            histories[secid] = {row["TRADEDATE"]: row for row in source["rows"]}
    return histories


def _ruonia(benchmark_cache: Path) -> tuple[list[str], dict[str, float]]:
    try:
        rows = json.loads(benchmark_cache.read_text(encoding="utf-8"))["rows"]
        rates = {row["date"]: float(row["rate"]) / 100 for row in rows}
    except (OSError, ValueError, KeyError) as exc:
        raise W0Error(f"cannot read RUONIA cache: {exc}") from exc
    return sorted(rates), rates


def _terms(contracts_path: Path) -> dict[str, dict[str, float]]:
    try:
        instruments = {r["secid"]: r for r in json.loads(contracts_path.read_text(encoding="utf-8"))["instruments"]}
        terms = {}
        for asset, secid in CURRENT_LEG.items():
            row = instruments[secid]
            mult = row["tick_value"] / row["tick_size"]
            terms[asset] = {"mult": mult, "margin_rate": row["initial_margin"] / (row["settlement"] * mult), "tick_value": row["tick_value"]}
        return terms
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise W0Error(f"cannot read current contract terms: {exc}") from exc


def _stress_ok(margin: float, quantity: int = 1) -> bool:
    buffers = (DEFAULT_STRESS_VM_BUFFER + DEFAULT_UNCERTAINTY_BUFFER) * quantity
    return 3 * margin * quantity + buffers <= INITIAL_CAPITAL


def build_windows(
    histories: dict[str, dict[str, dict[str, Any]]], asset: str, terms: dict[str, float],
    ruonia: tuple[list[str], dict[str, float]],
) -> list[dict[str, Any]]:
    """One window per complete quarter: days after the previous near expiry through two days before this one."""
    chain = contract_chain(asset)
    last_date = max(max(h) for h in histories.values() if h)
    ruonia_dates, rates = ruonia
    windows, previous_expiry = [], None
    for near, far in zip(chain, chain[1:]):
        legs, native = histories.get(near), histories.get(spread_id(near, far))
        far_rows = histories.get(far)
        if not legs or not native or not far_rows:
            continue
        expiry = max(legs)
        if expiry >= last_date:
            break
        expiry_date = date.fromisoformat(expiry)
        cutoff = (expiry_date - timedelta(days=EXIT_BUFFER_DAYS)).isoformat()
        days = [d for d in sorted(native) if (previous_expiry is None or d > previous_expiry) and d <= cutoff
                and d in legs and d in far_rows and legs[d]["SETTLEPRICE"] and far_rows[d]["SETTLEPRICE"]]
        previous_expiry = expiry
        if len(days) < 25:
            continue
        mult = terms["mult"]
        dividend = sum(
            value for ex, value in SBER_EX_DATES
            if asset == "SR" and expiry_date < ex <= expiry_date + timedelta(days=QUARTER_DAYS)
        ) * DIVIDEND_SHARES_PER_CONTRACT
        rows = []
        for d in days:
            near_value, far_value = legs[d]["SETTLEPRICE"] * mult, far_rows[d]["SETTLEPRICE"] * mult
            spread = far_value - near_value
            near_open, far_open = legs[d]["OPEN"] or 0, far_rows[d]["OPEN"] or 0
            rate = rates[ruonia_dates[max(0, bisect_right(ruonia_dates, d) - 1)]]
            implied = (spread + dividend) / near_value * 365 / QUARTER_DAYS
            rows.append({
                "date": d,
                "spread": spread,
                "open_spread": (far_open - near_open) * mult if near_open > 0 and far_open > 0 else spread,
                "near_close": near_value,
                "far_close": far_value,
                "dev_pp": (implied - rate) * 100 if ASSETS[asset] == "stock" else math.nan,
                "days_to_near_expiry": (expiry_date - date.fromisoformat(d)).days,
                "dividend_in_window": dividend > 0,
                "margin": terms["margin_rate"] * (near_value + far_value),
                "min_volume": float(native[d]["VOLUME"] or 0),
            })
        windows.append({
            "pair": f"{near}/{far}",
            "spread_instrument": spread_id(near, far),
            "first_date": days[0],
            "last_date": days[-1],
            "near_expiry": expiry,
            "rows": rows,
            "min_spread_volume": min(r["min_volume"] for r in rows),
            "max_pair_margin_rub": round(max(r["margin"] for r in rows), 2),
        })
    return windows


def _span_years(windows: list[dict[str, Any]]) -> float:
    return (date.fromisoformat(windows[-1]["near_expiry"]) - date.fromisoformat(windows[0]["first_date"])).days / 365


def run_calendar_study(
    cache_dir: Path, benchmark_path: Path, benchmark_cache_dir: Path, contracts_path: Path,
    *, assets: tuple[str, ...] = tuple(ASSETS), slippage_ticks: float = 1.0, till_date: str,
) -> dict[str, Any]:
    histories = fetch_inputs(cache_dir, assets, till_date)
    benchmark = read_benchmark(benchmark_path)
    ruonia = _ruonia(benchmark_cache_dir / "CBR_RUONIA.json")
    terms = _terms(contracts_path)
    profile = tariff_profile(slippage_ticks)
    report: dict[str, Any] = {}
    for asset in assets:
        windows = build_windows(histories, asset, terms[asset], ruonia)
        if not windows:
            report[asset] = {"status": "no_complete_windows"}
            continue
        eligible = {w["pair"]: w["rows"] for w in windows if w["min_spread_volume"] >= MIN_DAILY_VOLUME}
        years = _span_years(windows)
        variants = []
        for variant in evaluate_variants(eligible, benchmark, profile, native_spread=True, reference_cost=False,
                                         skip_dividend_windows_in_zscore=False, close_at_end=True) if eligible else []:
            by_window: dict[str, float] = {}
            for trade in variant.pop("trade_list"):
                by_window[trade["pair"]] = by_window.get(trade["pair"], 0.0) + trade["gross_rub"] - trade["costs_rub"]
            variants.append({
                **variant,
                "windows_with_trades": len(by_window),
                "windows_net_positive": sum(v > 0 for v in by_window.values()),
                "excess_pp_per_year_on_allocation": round(variant["excess_over_lqdt_on_margin_rub"] / INITIAL_CAPITAL / years * 100, 3),
            })
        report[asset] = {
            "status": "evaluated",
            "tariff_group": ASSETS[asset],
            "span": {"from": windows[0]["first_date"], "to": windows[-1]["near_expiry"], "years": round(years, 2)},
            "windows": [
                {
                    "pair": w["pair"], "spread_instrument": w["spread_instrument"], "from": w["first_date"], "to": w["last_date"],
                    "observations": len(w["rows"]), "min_spread_volume": w["min_spread_volume"],
                    "tradable_every_day": w["min_spread_volume"] >= MIN_DAILY_VOLUME,
                    "max_pair_margin_rub": w["max_pair_margin_rub"], "passes_capital_screen": _stress_ok(w["max_pair_margin_rub"]),
                }
                for w in windows
            ],
            "variants": variants,
        }
    return {
        "schema_version": "calendar-spread-study.v1",
        "release_state": "exploratory_inconclusive",
        "pricing": "spread = far leg settlement minus near leg settlement (identical to the exchange spread settlement where published); opens are leg opens",
        "execution": "native spread order: tariff on both legs, slippage once per side; next_open is the primary fill",
        "volume_rule": f"every day of the front window needs at least {MIN_DAILY_VOLUME} contracts in the spread instrument",
        "margin": "current margin-to-notional ratio of the live contract applied to historical settlements (fallback, A-05)",
        "dividends": "SBER ex-dates from a third-party seed database, unverified; GAZP has none in the seed",
        "benchmark": "LQDT lineage; excess subtracts LQDT return on pair margin while a position is open",
        "assets": report,
    }
