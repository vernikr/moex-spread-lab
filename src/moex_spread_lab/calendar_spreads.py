"""Native MOEX calendar-spread study over front windows, priced from official leg settlements."""

from __future__ import annotations

import json
import math
import re
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .account_risk import DEFAULT_STRESS_VM_BUFFER, DEFAULT_UNCERTAINTY_BUFFER
from .carry import CarryError, fetch_history
from .costs import TAKER_RATE_PERCENT, tariff_profile
from .s3_reference import FILLS, account_trades, evaluate_variants, read_benchmark, strategy_runner
from .screener_app import INITIAL_CAPITAL, MIN_DAILY_VOLUME
from .w0 import W0Error

QUARTER_CODES = "HMUZ"
MONTH_CODES = "FGHJKMNQUVXZ"
HISTORY_FIELDS = ("TRADEDATE", "SECID", "OPEN", "LOW", "HIGH", "CLOSE", "SETTLEPRICE", "VOLUME", "NUMTRADES")
ASSETS: dict[str, dict[str, Any]] = {
    "SR": {"group": "stock", "convergence": True, "name": "SBRF"},
    "GZ": {"group": "stock", "convergence": True, "name": "GAZR"},
    "LK": {"group": "stock", "name": "LKOH"},
    "RN": {"group": "stock", "name": "ROSN"},
    "SP": {"group": "stock", "name": "SBPR"},
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
FX_SOURCE = {"usd": "Si", "cny": "CR"}
EXIT_BUFFER_DAYS = 2
QUARTER_DAYS = 91
HISTORY_FROM = "2022-06-01"
SBER_EX_DATES = ((date(2023, 5, 9), 25.0), (date(2024, 7, 9), 33.3), (date(2025, 7, 16), 34.84), (date(2026, 7, 18), 37.64))
DIVIDEND_SHARES_PER_CONTRACT = 100
ZSCORE_OPTIONS = {
    "quarterly": {"window": 20, "max_hold_days": 30, "close_before": 7},
    "monthly": {"window": 7, "max_hold_days": 10, "close_before": 3},
}
STRATEGIES = ("convergence_to_ruonia", "zscore_mean_reversion")
FILL_LABELS = tuple(label for label, _delay, _fill in FILLS)


def contract_chain(asset: str, first_year: int = 3, last_year: int = 7) -> list[str]:
    monthly = ASSETS.get(asset, {}).get("monthly", False)
    codes, last = (MONTH_CODES, "F") if monthly else (QUARTER_CODES, "H")
    chain = [f"{asset}{code}{year}" for year in range(first_year, last_year + 1) for code in codes]
    return chain[:chain.index(f"{asset}{last}{last_year}") + 1]


def spread_id(near: str, far: str) -> str:
    return near + far


def fetch_inputs(
    cache_dir: Path, assets: tuple[str, ...], till_date: str, *, delay_seconds: float = 0.05, opener: Any = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """EOD history of every leg and consecutive native spread (plus FX sources), keyed by secid then date."""
    needed = set(assets) | {FX_SOURCE[ASSETS[a]["fx"]] for a in assets if ASSETS[a].get("fx")}
    extra = {"opener": opener} if opener is not None else {}
    secids = []
    for asset in sorted(needed):
        chain = contract_chain(asset)
        secids += [*chain, *(spread_id(n, f) for n, f in zip(chain, chain[1:]))]

    def load(secid: str) -> tuple[str, dict[str, dict[str, Any]] | None]:
        try:
            source = fetch_history(secid, HISTORY_FROM, till_date, cache_dir=cache_dir, delay_seconds=delay_seconds,
                                   fields=HISTORY_FIELDS, **extra)
        except CarryError:
            return secid, None
        return secid, {row["TRADEDATE"]: row for row in source["rows"]}

    with ThreadPoolExecutor(max_workers=1 if opener is not None else 6) as pool:
        return {secid: rows for secid, rows in pool.map(load, secids) if rows}


def cached_till_date(cache_dir: Path) -> str | None:
    found = {m.group(1) for path in cache_dir.glob("*.json") if (m := re.search(rf"-{HISTORY_FROM}-(\d{{4}}-\d{{2}}-\d{{2}})-\d+\.json$", path.name))}
    return max(found) if found else None


def _offline(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("offline: calendar history is read from the local cache only")


def _ruonia(benchmark_cache: Path) -> tuple[list[str], dict[str, float]]:
    try:
        rows = json.loads(benchmark_cache.read_text(encoding="utf-8"))["rows"]
        rates = {row["date"]: float(row["rate"]) / 100 for row in rows}
    except (OSError, ValueError, KeyError) as exc:
        raise W0Error(f"cannot read RUONIA cache: {exc}") from exc
    return sorted(rates), rates


def _terms(contracts_path: Path, assets: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    try:
        instruments = [r for r in json.loads(contracts_path.read_text(encoding="utf-8"))["instruments"]
                       if r.get("expiry") and r["expiry"] < "2100"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise W0Error(f"cannot read current contract terms: {exc}") from exc
    terms = {}
    for asset in assets:
        live = sorted((r for r in instruments if r["secid"].startswith(asset) and len(r["secid"]) == len(asset) + 2),
                      key=lambda r: r["expiry"])
        if not live:
            continue
        row = live[0]
        mult = row["tick_value"] / row["tick_size"]
        terms[asset] = {
            "mult": mult, "tick_value": row["tick_value"], "tick_size": row["tick_size"],
            "margin_rate": row["initial_margin"] / (row["settlement"] * mult), "reference_contract": row["secid"],
        }
    return terms


def _fx_factors(histories: dict[str, dict[str, dict[str, Any]]]) -> dict[str, dict[str, float]]:
    """RUB value of a point relative to today, from the front quarterly contract of Si (USD) or CR (CNY)."""
    factors: dict[str, dict[str, float]] = {}
    for kind, asset in FX_SOURCE.items():
        chain = [(max(histories[s]), s) for s in contract_chain(asset) if histories.get(s)]
        if not chain:
            continue
        dates = sorted({d for s in contract_chain(asset) for d in histories.get(s, {})})
        scale = 1000.0 if kind == "usd" else 1.0
        fronts = {}
        for day in dates:
            live = [s for expiry, s in sorted(chain) if expiry >= day and day in histories[s] and histories[s][day]["SETTLEPRICE"]]
            if live:
                fronts[day] = histories[live[0]][day]["SETTLEPRICE"] / scale
        if fronts:
            latest = fronts[max(fronts)]
            factors[kind] = {day: value / latest for day, value in fronts.items()}
    return factors


def _positive_spread_volume_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows with observed native spread executions; zero/missing volume is journal-only."""
    return [row for row in rows if row.get("min_volume") is not None and float(row["min_volume"]) > 0]


def _stress_ok(margin: float, quantity: int = 1) -> bool:
    buffers = (DEFAULT_STRESS_VM_BUFFER + DEFAULT_UNCERTAINTY_BUFFER) * quantity
    return 3 * margin * quantity + buffers <= INITIAL_CAPITAL


def build_windows(
    histories: dict[str, dict[str, dict[str, Any]]], asset: str, terms: dict[str, Any],
    ruonia: tuple[list[str], dict[str, float]], factors: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """One window per complete contract cycle: days after the previous near expiry through two days before this one."""
    config = ASSETS[asset]
    chain = contract_chain(asset)
    last_date = max(max(h) for h in histories.values() if h)
    ruonia_dates, rates = ruonia
    fx = (factors or {}).get(config.get("fx", ""), {})
    min_rows = 12 if config.get("monthly") else 25
    windows, previous_expiry = [], None
    for near, far in zip(chain, chain[1:]):
        legs, native, far_rows = histories.get(near), histories.get(spread_id(near, far)), histories.get(far)
        if not legs or not native or not far_rows:
            continue
        expiry = max(legs)
        if expiry >= last_date:
            break
        expiry_date = date.fromisoformat(expiry)
        cutoff = (expiry_date - timedelta(days=EXIT_BUFFER_DAYS)).isoformat()
        days = [d for d in sorted(native) if (previous_expiry is None or d > previous_expiry) and d <= cutoff
                and d in legs and d in far_rows and legs[d]["SETTLEPRICE"] and far_rows[d]["SETTLEPRICE"]
                and (not config.get("fx") or d in fx)]
        previous_expiry = expiry
        if len(days) < min_rows:
            continue
        dividend = sum(
            value for ex, value in SBER_EX_DATES
            if asset == "SR" and expiry_date < ex <= expiry_date + timedelta(days=QUARTER_DAYS)
        ) * DIVIDEND_SHARES_PER_CONTRACT
        rows = []
        for d in days:
            mult = terms["mult"] * fx.get(d, 1.0)
            near_px, far_px = legs[d]["SETTLEPRICE"], far_rows[d]["SETTLEPRICE"]
            near_open, far_open = legs[d]["OPEN"] or 0, far_rows[d]["OPEN"] or 0
            near_value, far_value = near_px * mult, far_px * mult
            spread = far_value - near_value
            rate = rates[ruonia_dates[max(0, bisect_right(ruonia_dates, d) - 1)]]
            implied = (spread + dividend) / near_value * 365 / QUARTER_DAYS
            rows.append({
                "date": d,
                "spread": spread,
                "open_spread": (far_open - near_open) * mult if near_open > 0 and far_open > 0 else spread,
                "near_close": near_value,
                "far_close": far_value,
                "dev_pp": (implied - rate) * 100 if config.get("convergence") else math.nan,
                "days_to_near_expiry": (expiry_date - date.fromisoformat(d)).days,
                "dividend_in_window": dividend > 0,
                "margin": terms["margin_rate"] * (near_value + far_value),
                "min_volume": float(native[d]["VOLUME"] or 0),
                "mult": mult,
                "near_price": near_px, "far_price": far_px, "near_open_price": near_open, "far_open_price": far_open,
                "spread_price": far_px - near_px,
                "spread_open_price": far_open - near_open if near_open > 0 and far_open > 0 else far_px - near_px,
                "spread_last_trade": native[d].get("CLOSE"),
                "spread_trades": native[d].get("NUMTRADES"),
                "near_volume": legs[d].get("VOLUME"), "far_volume": far_rows[d].get("VOLUME"),
            })
        eligible_volumes = [r["min_volume"] for r in _positive_spread_volume_rows(rows)]
        windows.append({
            "pair": f"{near}/{far}", "spread_instrument": spread_id(near, far), "near": near, "far": far,
            "first_date": days[0], "last_date": days[-1], "near_expiry": expiry, "rows": rows,
            "min_spread_volume": min(eligible_volumes) if eligible_volumes else 0.0,
            "max_pair_margin_rub": round(max(r["margin"] for r in rows), 2),
        })
    return windows


def _span_years(windows: list[dict[str, Any]]) -> float:
    return (date.fromisoformat(windows[-1]["near_expiry"]) - date.fromisoformat(windows[0]["first_date"])).days / 365


def _zscore_options(asset: str) -> dict[str, Any]:
    return ZSCORE_OPTIONS["monthly" if ASSETS[asset].get("monthly") else "quarterly"]


def run_calendar_study(
    cache_dir: Path, benchmark_path: Path, benchmark_cache_dir: Path, contracts_path: Path,
    *, assets: tuple[str, ...] = tuple(ASSETS), slippage_ticks: float = 1.0, till_date: str,
) -> dict[str, Any]:
    histories = fetch_inputs(cache_dir, assets, till_date)
    benchmark = read_benchmark(benchmark_path)
    ruonia = _ruonia(benchmark_cache_dir / "CBR_RUONIA.json")
    terms = _terms(contracts_path, assets)
    factors = _fx_factors(histories)
    profile = tariff_profile(slippage_ticks)
    report: dict[str, Any] = {}
    for asset in assets:
        windows = build_windows(histories, asset, terms[asset], ruonia, factors) if asset in terms else []
        if not windows:
            report[asset] = {"status": "no_complete_windows"}
            continue
        eligible = {w["pair"]: w["rows"] for w in windows if w["min_spread_volume"] >= MIN_DAILY_VOLUME}
        years = _span_years(windows)
        variants = []
        for variant in evaluate_variants(
            eligible, benchmark, {**profile, "tariff_group": ASSETS[asset]["group"]}, native_spread=True, reference_cost=False,
            skip_dividend_windows_in_zscore=False, close_at_end=True, zscore_options=_zscore_options(asset),
        ) if eligible else []:
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
            "name": ASSETS[asset]["name"],
            "tariff_group": ASSETS[asset]["group"],
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
        "schema_version": "calendar-spread-study.v2",
        "release_state": "exploratory_inconclusive",
        "pricing": "spread = far leg settlement minus near leg settlement (identical to the exchange spread settlement where published); opens are leg opens",
        "execution": "native spread order: tariff on both legs, slippage once per side; next_open is the primary fill",
        "volume_rule": f"every day of the front window needs at least {MIN_DAILY_VOLUME} contracts in the spread instrument",
        "margin": "current margin-to-notional ratio of the live contract applied to historical settlements (fallback, A-05)",
        "fx": "USD- and CNY-quoted point values are rescaled by the front Si or CR settlement relative to today (approximation)",
        "dividends": "SBER ex-dates from a third-party seed database, unverified; none for other stocks (convergence runs only for SR and GZ)",
        "benchmark": "LQDT lineage; excess subtracts LQDT return on pair margin while a position is open",
        "assets": report,
    }


class CalendarData:
    """Offline view of the cached calendar history for the proof screen."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.cache = self.root / "data/spread-history-cache"
        self._loaded: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._loaded is None:
            till = cached_till_date(self.cache)
            if till is None:
                raise W0Error("calendar history is not cached; run calendar-study first")
            histories = fetch_inputs(self.cache, tuple(ASSETS), till, delay_seconds=0, opener=_offline)
            terms = _terms(self.root / "data/w1/current-intake.json", tuple(ASSETS))
            ruonia = _ruonia(self.root / "data/benchmark-cache/CBR_RUONIA.json")
            factors = _fx_factors(histories)
            windows = {a: build_windows(histories, a, terms[a], ruonia, factors) for a in ASSETS if a in terms and any(
                s in histories for s in contract_chain(a))}
            self._loaded = {"windows": windows, "terms": terms, "till": till,
                            "benchmark": read_benchmark(self.root / "data/benchmark/lqdt-lineage.json")}
        return self._loaded

    def catalog(self) -> dict[str, Any]:
        data = self._load()
        return {
            "till": data["till"],
            "min_daily_volume": MIN_DAILY_VOLUME,
            "strategies": STRATEGIES,
            "fills": FILL_LABELS,
            "assets": [
                {
                    "asset": asset, "name": ASSETS[asset]["name"], "group": ASSETS[asset]["group"],
                    "monthly": bool(ASSETS[asset].get("monthly")),
                    "convergence": bool(ASSETS[asset].get("convergence")),
                    "windows": [
                        {"pair": w["pair"], "from": w["first_date"], "to": w["last_date"], "observations": len(w["rows"]),
                         "min_spread_volume": w["min_spread_volume"], "tradable_every_day": w["min_spread_volume"] >= MIN_DAILY_VOLUME,
                         "max_pair_margin_rub": w["max_pair_margin_rub"], "passes_capital_screen": _stress_ok(w["max_pair_margin_rub"])}
                        for w in windows
                    ],
                }
                for asset, windows in data["windows"].items() if windows
            ],
        }

    def proof(self, selection: dict[str, Any]) -> dict[str, Any]:
        data = self._load()
        asset, pair = selection.get("asset"), selection.get("pair")
        strategy, fill_label = selection.get("strategy", "zscore_mean_reversion"), selection.get("fill", "next_open")
        quantity, slippage = selection.get("quantity", 1), selection.get("slippage_ticks", 1.0)
        if asset not in ASSETS or strategy not in STRATEGIES or fill_label not in FILL_LABELS:
            raise W0Error("unknown asset, strategy or fill rule")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 50:
            raise W0Error("contracts must be an integer between 1 and 50")
        window = next((w for w in data["windows"].get(asset, []) if w["pair"] == pair), None)
        if window is None:
            raise W0Error("no complete front window for this pair")
        delay, fill = next((d, f) for label, d, f in FILLS if label == fill_label)
        options = {"entry_z": 1.5, "exit_z": 0.5, **_zscore_options(asset)}
        params = selection.get("parameters", {})
        if not isinstance(params, dict) or set(params) - {"entry_z", "exit_z", "window", "threshold_pp"}:
            raise W0Error("unknown strategy parameter")
        for key in ("entry_z", "exit_z", "window"):
            if key in params:
                options[key] = params[key]
        threshold = float(params.get("threshold_pp", 1.0))
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 for v in (*options.values(), threshold)):
            raise W0Error("strategy parameters must be positive finite numbers")
        options["window"] = int(options["window"])
        profile = {**tariff_profile(float(slippage)), "tariff_group": ASSETS[asset]["group"]}
        runner = strategy_runner(strategy, delay=delay, fill=fill, close_at_end=True, skip_dividend_windows_in_zscore=False,
                                 threshold=threshold, zscore_options=options)
        # Zero-volume native spread rows are journal-only: never feed them to the signal or fill simulator.
        analysis_rows = _positive_spread_volume_rows(window["rows"])
        if len(analysis_rows) < 2:
            raise W0Error("fewer than two positive-volume calendar-spread observations")
        per_pair = {pair: runner(analysis_rows)}
        account = account_trades("proof", per_pair, data["benchmark"], profile, native_spread=True, quantity=quantity)
        return _proof_payload(window, account, asset, strategy, fill_label, options, threshold, quantity, float(slippage),
                              data["terms"][asset], profile)


def _rel_pct(value: Any, base: Any) -> float | None:
    """Spread as % of the far price. Pure same-row ratio — strictly causal."""
    try:
        numerator = float(value)
        denominator = float(base)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator == 0.0:
        return None
    return round(numerator / denominator * 100, 6)


def _clean(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _proof_payload(window, account, asset, strategy, fill_label, options, threshold, quantity, slippage, terms, profile) -> dict[str, Any]:
    rows = window["rows"]
    trades = []
    for number, trade in enumerate(account["trade_list"], start=1):
        entry, exit_row, side = trade["entry_row"], trade["exit_row"], trade["direction"]
        entry_fee, exit_fee = trade["entry_costs"], trade["exit_costs"]
        trades.append({
            "number": number,
            "side": "short_spread" if side < 0 else "long_spread",
            "legs": "sell far, buy near" if side < 0 else "buy far, sell near",
            "quantity": quantity,
            "signal": trade["signal"],
            "entry": {"date": entry["date"], "spread_fill": entry["spread_open_price"] if fill_label == "next_open" else entry["spread_price"],
                      "spread_fill_rel": _rel_pct(entry["spread_open_price"] if fill_label == "next_open" else entry["spread_price"], entry["far_open_price"] if fill_label == "next_open" else entry["far_price"]),
                      "near_price": entry["near_open_price"] if fill_label == "next_open" else entry["near_price"],
                      "far_price": entry["far_open_price"] if fill_label == "next_open" else entry["far_price"],
                      "spread_volume": entry["min_volume"], "near_volume": entry["near_volume"], "far_volume": entry["far_volume"],
                      "costs": entry_fee},
            "exit": {"date": exit_row["date"], "spread_fill": exit_row["spread_open_price"] if fill_label == "next_open" else exit_row["spread_price"],
                     "spread_fill_rel": _rel_pct(exit_row["spread_open_price"] if fill_label == "next_open" else exit_row["spread_price"], exit_row["far_open_price"] if fill_label == "next_open" else exit_row["far_price"]),
                     "near_price": exit_row["near_open_price"] if fill_label == "next_open" else exit_row["near_price"],
                     "far_price": exit_row["far_open_price"] if fill_label == "next_open" else exit_row["far_price"],
                     "spread_volume": exit_row["min_volume"], "near_volume": exit_row["near_volume"], "far_volume": exit_row["far_volume"],
                     "costs": exit_fee},
            "reason": trade["reason"],
            "days_held": (date.fromisoformat(exit_row["date"]) - date.fromisoformat(entry["date"])).days,
            "margin_rub": trade["margin_rub"],
            "gross_rub": trade["gross_rub"],
            "costs_rub": trade["costs_rub"],
            "net_rub": round(trade["gross_rub"] - trade["costs_rub"], 2),
            "lqdt_on_margin_rub": trade["lqdt_on_margin_rub"],
            "excess_rub": round(trade["gross_rub"] - trade["costs_rub"] - trade["lqdt_on_margin_rub"], 2),
        })
    ledger, position, cumulative = [], None, 0.0
    by_entry = {t["entry"]["date"]: t for t in trades}
    by_exit = {t["exit"]["date"]: t for t in trades}
    marked = {t["number"]: 0.0 for t in trades}
    for row in rows:
        day, daily, cost = row["date"], 0.0, 0.0
        point = row["mult"] * quantity
        volume_eligible = row.get("min_volume") is not None and row["min_volume"] > 0
        if volume_eligible and position is not None:
            exiting = by_exit.get(day)
            end = exiting["exit"]["spread_fill"] if exiting else row["spread_price"]
            move = position["sign"] * (end - position["mark"]) * point
            daily += move
            marked[position["number"]] += move
            if exiting:
                cost += sum(exiting["exit"]["costs"].values())
                position = None
            else:
                position["mark"] = row["spread_price"]
        entering = by_entry.get(day) if volume_eligible else None
        if entering:
            sign = 1 if entering["side"] == "long_spread" else -1
            cost += sum(entering["entry"]["costs"].values())
            move = sign * (row["spread_price"] - entering["entry"]["spread_fill"]) * point
            daily += move
            marked[entering["number"]] += move
            position = {"sign": sign, "mark": row["spread_price"], "number": entering["number"]}
        cumulative += daily - cost
        ledger.append({
            "date": day, "position": position["sign"] if position else 0, "mark_pnl_rub": round(daily, 2),
            "costs_rub": round(cost, 2), "cumulative_net_rub": round(cumulative, 2),
        })
    fx_drift = 0.0
    for trade in trades:
        static = trade["gross_rub"]
        trade["gross_rub_static"] = static
        trade["gross_rub"] = round(marked[trade["number"]], 2)
        trade["net_rub"] = round(trade["gross_rub"] - trade["costs_rub"], 2)
        trade["excess_rub"] = round(trade["net_rub"] - trade["lqdt_on_margin_rub"], 2)
        fx_drift += trade["gross_rub"] - static
    gross = sum(t["gross_rub"] for t in trades)
    net = sum(t["net_rub"] for t in trades)
    reconciliation = round(net - cumulative, 2)
    return _clean({
        "schema_version": "calendar-proof.v1",
        "selection": {"asset": asset, "pair": window["pair"], "spread_instrument": window["spread_instrument"], "strategy": strategy,
                      "fill": fill_label, "quantity": quantity, "slippage_ticks": slippage,
                      "parameters": {**options, "threshold_pp": threshold}},
        "window": {"from": window["first_date"], "to": window["last_date"], "near_expiry": window["near_expiry"],
                   "near": window["near"], "far": window["far"], "min_spread_volume": window["min_spread_volume"],
                   "tradable_every_day": window["min_spread_volume"] >= MIN_DAILY_VOLUME,
                   "min_daily_volume_rule": MIN_DAILY_VOLUME,
                   "excluded_volume_observations": sum(r.get("min_volume") is None or r["min_volume"] <= 0 for r in rows),
                   "passes_capital_screen": _stress_ok(window["max_pair_margin_rub"], quantity),
                   "max_pair_margin_rub": window["max_pair_margin_rub"]},
        "contract": {"point_value_rub": terms["mult"], "tick_size": terms["tick_size"], "reference_contract": terms["reference_contract"],
                     "tariff_group": ASSETS[asset]["group"],
                     "taker_rate_percent": TAKER_RATE_PERCENT[ASSETS[asset]["group"]], "broker_fee_rub": 0.0},
        "series": [
            {"date": r["date"], "near": r["near_price"], "far": r["far_price"], "near_open": r["near_open_price"],
             "far_open": r["far_open_price"], "spread": r["spread_price"], "spread_open": r["spread_open_price"],
             "spread_rel": _rel_pct(r["spread_price"], r["far_price"]),
             "spread_open_rel": _rel_pct(r["spread_open_price"], r["far_open_price"]),
             "spread_last_trade_rel": _rel_pct(r["spread_last_trade"], r["far_price"]),
             "spread_last_trade": r["spread_last_trade"], "spread_volume": r["min_volume"], "volume_eligible": r["min_volume"] > 0,
             "spread_trades": r["spread_trades"],
             "near_volume": r["near_volume"], "far_volume": r["far_volume"], "days_to_near_expiry": r["days_to_near_expiry"],
             "dev_pp": r["dev_pp"], "margin_rub": round(r["margin"] * quantity, 2)}
            for r in rows
        ],
        "trades": trades,
        "ledger": ledger,
        "summary": {
            "trades": len(trades), "gross_rub": round(gross, 2), "costs_rub": account["costs_rub"], "net_rub": round(net, 2),
            "lqdt_on_margin_rub": account["lqdt_on_pair_margin_while_open_rub"],
            "excess_over_lqdt_rub": round(net - account["lqdt_on_pair_margin_while_open_rub"], 2),
            "winners": sum(t["net_rub"] > 0 for t in trades), "ledger_reconciliation_rub": reconciliation,
            "fx_point_value_drift_rub": round(fx_drift, 2),
        },
        "notes": [
            "Цена спреда = расчёт дальней минус расчёт ближней (биржевой расчёт спреда там, где он публикуется).",
            "Даты с нулевым объёмом самого календарного спреда остаются в журнале как наблюдения, но исключены из индикаторов, гейта объёма и симуляции входа/выхода; позиции на них не переоцениваются.",
            "Исполнение next_open использует цены первых сделок ног за день; исполнение settlement — официальный расчёт.",
            "Отметки — по расчётным ценам с дневной стоимостью пункта; издержки — опубликованный тариф биржи по обеим ногам плюс slippage один раз на сторону.",
            "Для контрактов в USD и CNY стоимость пункта дрейфует вместе с валютой, поэтому день-в-день результат немного отличается от арифметики «вход–выход» (показано как дрейф стоимости пункта FX).",
            "Маржа — запасной коэффициент по сегодняшнему контракту; издержки LQDT — доходность бенчмарка на этой марже, пока позиция открыта.",
            "Спред показан в % от цены дальней ноги; абсолютные пункты остаются в CSV и во второй строке таблицы.",
        ],
    })
