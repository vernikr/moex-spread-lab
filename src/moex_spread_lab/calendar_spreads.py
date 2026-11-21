"""Native MOEX calendar-spread study over front windows, priced from official leg settlements."""

from __future__ import annotations

import itertools
import json
import math
import re
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .account_risk import DEFAULT_STRESS_VM_BUFFER, DEFAULT_UNCERTAINTY_BUFFER
from .carry import CarryError, fetch_history
from .costs import TAKER_RATE_PERCENT, tariff_profile
from .data_lake import DataLakeError, MarketDataStore
from .s3_reference import (
    FILLS,
    account_trades,
    evaluate_variants,
    read_benchmark,
    strategy_runner,
)
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
    "MG": {"group": "stock", "name": "MGNT"},
    "YN": {"group": "stock", "name": "YDEX"},
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
FX_SOURCE = {"usd": "Si", "cny": "CR"}
CROSS_EXPIRY_PAIRS: tuple[dict[str, str], ...] = (
    {"pair_id": "MX/MM", "near": "MX", "far": "MM", "family": "cross_contract_same_expiry", "name": "MIX vs MXI"},
    {"pair_id": "GD/GN", "near": "GD", "far": "GN", "family": "cross_contract_same_expiry", "name": "GOLD vs GOLDM"},
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

# H1 calendars use explicit session liquidity screens. The units differ by price source:
# native spread volume versus the minimum matched-bar volume of the two legs.
MIN_H1_CALENDAR_OBSERVATIONS = 22
MIN_H1_NATIVE_SESSION_VOLUME = MIN_DAILY_VOLUME
MIN_H1_PROXY_LEG_SESSION_VOLUME = MIN_DAILY_VOLUME


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
        secids += [*chain, *(spread_id(n, f) for n, f in itertools.pairwise(chain))]

    def load(secid: str) -> tuple[str, dict[str, dict[str, Any]] | None]:
        try:
            source = fetch_history(secid, HISTORY_FROM, till_date, cache_dir=cache_dir, delay_seconds=delay_seconds,
                                   fields=HISTORY_FIELDS, **extra)
        except CarryError:
            return secid, None
        return secid, {row["TRADEDATE"]: row for row in source["rows"]}

    if opener is not None:
        out = {}
        for secid in secids:
            _, rows = load(secid)
            if rows:
                out[secid] = rows
        return out
    with ThreadPoolExecutor(max_workers=6) as pool:
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
        # A failed stress screen is not the same thing as missing margin/point values.
        required = (row.get(k) for k in ("tick_value", "tick_size", "initial_margin", "settlement"))
        if not all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in required):
            continue
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


def _stress_ok(margin: float, quantity: int = 1, initial_capital: float = INITIAL_CAPITAL) -> bool:
    buffers = (DEFAULT_STRESS_VM_BUFFER + DEFAULT_UNCERTAINTY_BUFFER) * quantity
    return 3 * margin * quantity + buffers <= initial_capital


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
    for near, far in itertools.pairwise(chain):
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
    """Offline view of cached daily and hourly calendar-spread inputs."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.cache = self.root / "data/spread-history-cache"
        self._loaded: dict[str, Any] | None = None
        self.bar_store = MarketDataStore(self.root)
        self._h1_candle_cache: dict[str, tuple[tuple[Any, ...], tuple[dict[str, Any] | None, str | None]]] = {}
        self._h1_windows: dict[str, list[dict[str, Any]]] = {}

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
            benchmark = read_benchmark(self.root / "data/benchmark/lqdt-lineage.json")
            self._loaded = {
                "windows": windows,
                "terms": terms,
                "till": till,
                "ruonia": ruonia,
                "fx_factors": factors,
                "benchmark": benchmark,
                "benchmark_dates": sorted(benchmark),
            }
        return self._loaded

    @staticmethod
    def _candle_timestamp(value: Any) -> str | None:
        if not isinstance(value, str) or len(value) < 16:
            return None
        timestamp = value[:19].replace(" ", "T")
        try:
            datetime.fromisoformat(timestamp)
        except ValueError:
            return None
        return timestamp

    @staticmethod
    def _number(value: Any) -> float | None:
        if type(value) not in (int, float):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    def _read_h1_candles(self, secid: str) -> tuple[dict[str, Any] | None, str | None]:
        path = self.bar_store.select_h1(secid)
        if not self.bar_store.source_available(path):
            return None, "no_local_h1_file"
        try:
            source = self.bar_store.read(path, columns=("begin", "open", "close", "volume"))
            fingerprint = source.cache_key
            cached = self._h1_candle_cache.get(secid)
            if cached and cached[0] == fingerprint:
                return cached[1]
            if source.schema_version != "iss-candles.v1" or source.interval != 60:
                result = (None, "invalid_h1_schema_or_interval")
            elif source.secid not in (None, secid):
                result = (None, "secid_mismatch")
            elif not source.rows:
                result = (None, "empty_h1_series")
            else:
                rows: dict[str, dict[str, Any]] = {}
                for source_row in source.rows:
                    timestamp = self._candle_timestamp(source_row.get("begin"))
                    close = self._number(source_row.get("close"))
                    if timestamp is None or close is None:
                        continue
                    opening = self._number(source_row.get("open"))
                    volume = self._number(source_row.get("volume"))
                    rows[timestamp] = {
                        "begin": timestamp,
                        "open": opening,
                        "close": close,
                        "volume": volume if volume is not None and volume >= 0 else None,
                    }
                if not rows:
                    result = (None, "no_valid_h1_rows")
                else:
                    result = ({
                        "secid": secid,
                        "path": source.source_path,
                        "sha256": source.source_sha256,
                        "rows": rows,
                    }, None)
            self._h1_candle_cache[secid] = (fingerprint, result)
            return result
        except (DataLakeError, OSError, TypeError, ValueError):
            return None, "unreadable_h1_series"

    @staticmethod
    def _asof_before(days: list[str], values: dict[str, float], day: str) -> tuple[str | None, float | None]:
        index = bisect_left(days, day) - 1
        if index < 0:
            return None, None
        key = days[index]
        return key, values.get(key)

    def _intraday_window(self, asset: str, daily_window: dict[str, Any], data: dict[str, Any]) -> dict[str, Any] | None:
        near, far = daily_window["near"], daily_window["far"]
        near_source, near_error = self._read_h1_candles(near)
        far_source, far_error = self._read_h1_candles(far)
        if near_source is None or far_source is None:
            return None

        first_day, last_day = daily_window["first_date"][:10], daily_window["last_date"][:10]
        near_rows = {
            stamp: row for stamp, row in near_source["rows"].items()
            if first_day <= stamp[:10] <= last_day and row["close"] > 0
        }
        far_rows = {
            stamp: row for stamp, row in far_source["rows"].items()
            if first_day <= stamp[:10] <= last_day and row["close"] > 0
        }
        common = sorted(near_rows.keys() & far_rows.keys())
        if len(common) < 2:
            return None

        native_id = spread_id(near, far)
        native_source, native_error = self._read_h1_candles(native_id)
        native_rows = native_source["rows"] if native_source else {}
        native_common = [stamp for stamp in common if stamp in native_rows]
        if len(native_common) >= MIN_H1_CALENDAR_OBSERVATIONS:
            price_source = "native_h1_spread"
            source_rows = native_rows
            fallback_reason = None
            gate = MIN_H1_NATIVE_SESSION_VOLUME
            liquidity_source = "native_spread_session_volume"
            liquidity_volume_label = "минимум дневного объёма нативного спреда"
            liquidity_rule = "Сумма объёма свечей нативного календарного спреда за сессию; каждый часовой бар исполним только при достаточном объёме."
        else:
            price_source = "synthetic_leg_proxy"
            source_rows = {}
            fallback_reason = native_error or ("native_h1_insufficient_overlap" if native_source else "native_h1_unavailable")
            gate = MIN_H1_PROXY_LEG_SESSION_VOLUME
            liquidity_source = "matched_near_far_session_volume"
            liquidity_volume_label = "минимум суммарных объёмов синхронных баров обеих ног за сессию"
            liquidity_rule = "Прокси far − near: минимум суммарных объёмов ближней и дальней ноги на синхронных часовых отметках; бар исполним при объёме обеих ног."

        session_days = sorted({stamp[:10] for stamp in common})
        session_volume: dict[str, float] = {day: 0.0 for day in session_days}
        unknown_volume: set[str] = set()
        if price_source == "native_h1_spread":
            for stamp in native_common:
                value = source_rows[stamp].get("volume")
                if value is None:
                    unknown_volume.add(stamp[:10])
                else:
                    session_volume[stamp[:10]] += value
        else:
            near_session: dict[str, float] = {day: 0.0 for day in session_days}
            far_session: dict[str, float] = {day: 0.0 for day in session_days}
            for stamp in common:
                nv, fv = near_rows[stamp].get("volume"), far_rows[stamp].get("volume")
                if nv is None or fv is None:
                    unknown_volume.add(stamp[:10])
                else:
                    near_session[stamp[:10]] += nv
                    far_session[stamp[:10]] += fv
            session_volume = {day: min(near_session[day], far_session[day]) for day in session_days}

        session_ok = {
            day: day not in unknown_volume and session_volume.get(day, 0.0) >= gate
            for day in session_days
        }
        min_liquidity = min(session_volume.values()) if session_volume else 0.0
        tradable = bool(session_ok) and all(session_ok.values())

        terms = data["terms"][asset]
        config = ASSETS[asset]
        expiry = daily_window["near_expiry"]
        expiry_date = date.fromisoformat(expiry)
        dividend = sum(
            value for ex, value in SBER_EX_DATES
            if asset == "SR" and expiry_date < ex <= expiry_date + timedelta(days=QUARTER_DAYS)
        ) * DIVIDEND_SHARES_PER_CONTRACT
        ruonia_dates, rates = data["ruonia"]
        benchmark_dates = data["benchmark_dates"]
        fx_name = config.get("fx")
        fx_values = data["fx_factors"].get(fx_name, {}) if fx_name else {}
        fx_dates = sorted(fx_values)
        days_to_expiry_cache: dict[str, int] = {}
        cumulative_native: dict[str, float] = {}
        cumulative_near: dict[str, float] = {}
        cumulative_far: dict[str, float] = {}
        previous_bar_volume: dict[str, float] = {}
        rows: list[dict[str, Any]] = []
        observation_stamps = native_common if price_source == "native_h1_spread" else common

        for stamp in observation_stamps:
            session_day = stamp[:10]
            nr, fr = near_rows[stamp], far_rows[stamp]
            near_raw, far_raw = nr["close"], fr["close"]
            near_open_raw = nr["open"] if nr["open"] is not None and nr["open"] > 0 else near_raw
            far_open_raw = fr["open"] if fr["open"] is not None and fr["open"] > 0 else far_raw
            if fx_name:
                fx_date, fx_factor = self._asof_before(fx_dates, fx_values, session_day)
                if fx_factor is None:
                    continue
            else:
                fx_date, fx_factor = None, 1.0
            mult = terms["mult"] * fx_factor
            near_value, far_value = near_raw * mult, far_raw * mult
            if near_value <= 0 or far_value <= 0:
                continue

            if price_source == "native_h1_spread":
                native = source_rows[stamp]
                spread_raw = native["close"]
                spread_open_raw = native["open"]
                if spread_open_raw is None:
                    spread_open_raw = far_open_raw - near_open_raw
                min_volume = native.get("volume") or 0.0
                min_volume = max(0.0, min_volume)
                spread_volume = native.get("volume")
            else:
                spread_raw = far_raw - near_raw
                spread_open_raw = far_open_raw - near_open_raw
                nv, fv = nr.get("volume"), fr.get("volume")
                min_volume = min(nv, fv) if nv is not None and fv is not None else 0.0
                spread_volume = None

            rate_date, rate = self._asof_before(ruonia_dates, rates, session_day)
            benchmark_index = bisect_left(benchmark_dates, session_day) - 1
            if benchmark_index < 0:
                continue
            benchmark_date = benchmark_dates[benchmark_index]
            spread_value = spread_raw * mult
            open_spread_value = spread_open_raw * mult
            if rate is not None:
                implied = (spread_value + dividend) / near_value * 365 / QUARTER_DAYS
                dev_pp = (implied - rate) * 100 if config.get("convergence") else math.nan
            else:
                dev_pp = math.nan

            if price_source == "native_h1_spread":
                before = cumulative_native.get(session_day, 0.0)
                current = before + min_volume
                prior_bar = previous_bar_volume.get(session_day, 0.0)
                cumulative_native[session_day] = current
            else:
                near_before = cumulative_near.get(session_day, 0.0)
                far_before = cumulative_far.get(session_day, 0.0)
                before = min(near_before, far_before)
                nv, fv = nr.get("volume"), fr.get("volume")
                nv = nv if nv is not None else 0.0
                fv = fv if fv is not None else 0.0
                cumulative_near[session_day] = near_before + nv
                cumulative_far[session_day] = far_before + fv
                current = min(cumulative_near[session_day], cumulative_far[session_day])
                prior_bar = previous_bar_volume.get(session_day, 0.0)
                previous_bar_volume[session_day] = min(nv, fv)
            if price_source == "native_h1_spread":
                previous_bar_volume[session_day] = min_volume

            margin = terms["margin_rate"] * (near_value + far_value)
            rows.append({
                "date": stamp,
                "timestamp": stamp,
                "session_date": session_day,
                "benchmark_mark_date": benchmark_date,
                "spread": spread_value,
                "open_spread": open_spread_value,
                "spread_price": spread_raw,
                "spread_open_price": spread_open_raw,
                "near_close": near_value,
                "far_close": far_value,
                "near_price": near_raw,
                "far_price": far_raw,
                "near_open_price": near_open_raw,
                "far_open_price": far_open_raw,
                "near_open": near_open_raw,
                "far_open": far_open_raw,
                "near_volume": nr.get("volume"),
                "far_volume": fr.get("volume"),
                "spread_volume": spread_volume,
                "spread_trades": None,
                "spread_last_trade": None,
                "min_volume": min_volume,
                "session_liquidity_volume": session_volume.get(session_day, 0.0),
                "liquidity_cumulative_before": before,
                "liquidity_cumulative": current,
                "prior_bar_volume": prior_bar,
                "volume_eligible": current >= gate and min_volume > 0,
                "signal_eligible": current >= gate and min_volume > 0,
                "dev_pp": dev_pp,
                "ruonia_rate": rate,
                "ruonia_rate_date": rate_date,
                "fx_rate_date": fx_date,
                "days_to_near_expiry": days_to_expiry_cache.setdefault(
                    session_day, (expiry_date - date.fromisoformat(session_day)).days
                ),
                "dividend_in_window": dividend > 0,
                "margin": margin,
                "mult": mult,
                "near_price_raw": near_raw,
                "far_price_raw": far_raw,
                "spread_open": open_spread_value,
            })

        if len(rows) < MIN_H1_CALENDAR_OBSERVATIONS:
            return None
        rows.sort(key=lambda row: row["date"])
        source_hashes = {
            "near_candles_sha256": near_source["sha256"],
            "far_candles_sha256": far_source["sha256"],
        }
        if native_source:
            source_hashes["native_spread_candles_sha256"] = native_source["sha256"]
        min_spread_volume = min_liquidity if price_source == "native_h1_spread" else None
        return {
            "pair": daily_window["pair"],
            "spread_instrument": native_id,
            "near": near,
            "far": far,
            "first_date": rows[0]["date"],
            "last_date": rows[-1]["date"],
            "near_expiry": expiry,
            "rows": rows,
            "observations": len(rows),
            "min_spread_volume": min_spread_volume,
            "min_liquidity_volume": min_liquidity,
            "max_pair_margin_rub": round(max(row["margin"] for row in rows), 2),
            "timeframe": "1h",
            "price_source": price_source,
            "price_source_label": (
                "Нативные часовые свечи календарного спреда"
                if price_source == "native_h1_spread"
                else "Синтетический прокси far − near по синхронизированным часовым свечам ног"
            ),
            "native_fallback_reason": fallback_reason,
            "has_native_spread_volume": price_source == "native_h1_spread",
            "native_spread_order": price_source == "native_h1_spread",
            "liquidity_source": liquidity_source,
            "liquidity_gate": gate,
            "liquidity_gate_unit": "контр./сессию",
            "liquidity_volume_label": liquidity_volume_label,
            "liquidity_rule": liquidity_rule,
            "tradable_every_day": tradable,
            "tradable_every_session": tradable,
            "session_count": len(session_days),
            "source_hashes": source_hashes,
        }

    def _intraday_windows_for(self, asset: str) -> list[dict[str, Any]]:
        if asset not in self._h1_windows:
            data = self._load()
            if asset not in ASSETS or asset not in data["terms"]:
                self._h1_windows[asset] = []
            else:
                windows = []
                for daily_window in data["windows"].get(asset, []):
                    window = self._intraday_window(asset, daily_window, data)
                    if window is not None:
                        windows.append(window)
                self._h1_windows[asset] = windows
        return self._h1_windows[asset]

    def _intraday_till(self, windows: list[dict[str, Any]]) -> str | None:
        summary_path = self.root / "data/w1/candles/summary.json"
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if summary.get("interval") == 60 and isinstance(summary.get("till"), str):
                return summary["till"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            pass
        last = max((window["last_date"] for window in windows), default=None)
        return last[:10] if last else None

    def catalog(self, *, initial_capital: float = INITIAL_CAPITAL, timeframe: str = "1d") -> dict[str, Any]:
        if timeframe == "1h":
            data = self._load()
            assets = []
            all_windows = []
            for asset in ASSETS:
                windows = self._intraday_windows_for(asset)
                all_windows.extend(windows)
                if not windows:
                    continue
                assets.append({
                    "asset": asset,
                    "name": ASSETS[asset]["name"],
                    "group": ASSETS[asset]["group"],
                    "monthly": bool(ASSETS[asset].get("monthly")),
                    "convergence": bool(ASSETS[asset].get("convergence")),
                    "windows": [
                        {
                            "pair": window["pair"],
                            "from": window["first_date"],
                            "to": window["last_date"],
                            "observations": window["observations"],
                            "timeframe": "1h",
                            "price_source": window["price_source"],
                            "price_source_label": window["price_source_label"],
                            "native_fallback_reason": window["native_fallback_reason"],
                            "has_native_spread_volume": window["has_native_spread_volume"],
                            "min_spread_volume": window["min_spread_volume"],
                            "min_liquidity_volume": window["min_liquidity_volume"],
                            "liquidity_source": window["liquidity_source"],
                            "liquidity_gate": window["liquidity_gate"],
                            "liquidity_gate_unit": window["liquidity_gate_unit"],
                            "liquidity_volume_label": window["liquidity_volume_label"],
                            "liquidity_rule": window["liquidity_rule"],
                            "min_daily_volume_rule": window["liquidity_gate"],
                            "tradable_every_day": window["tradable_every_day"],
                            "tradable_every_session": window["tradable_every_session"],
                            "session_count": window["session_count"],
                            "max_pair_margin_rub": window["max_pair_margin_rub"],
                            "passes_capital_screen": _stress_ok(window["max_pair_margin_rub"], initial_capital=initial_capital),
                        }
                        for window in windows
                    ],
                })
            return {
                "till": self._intraday_till(all_windows),
                "timeframe": "1h",
                "min_daily_volume": MIN_DAILY_VOLUME,
                "strategies": STRATEGIES,
                "fills": FILL_LABELS,
                "assets": assets,
                "notes": [
                    "Для 1h используются только точные пересечения локальных часовых баров; дневные свечи не подставляются.",
                    "Нативный spread H1 имеет приоритет при наличии достаточной синхронной истории; иначе цена явно помечена как синтетический far − near proxy.",
                    "Proxy gate считает объём обеих ног по синхронизированным часовым отметкам отдельно от нативного spread-volume gate.",
                ],
            }
        if timeframe != "1d":
            raise W0Error("unsupported calendar timeframe")
        data = self._load()
        return {
            "till": data["till"],
            "timeframe": "1d",
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
                         "max_pair_margin_rub": w["max_pair_margin_rub"], "passes_capital_screen": _stress_ok(w["max_pair_margin_rub"], initial_capital=initial_capital)}
                        for w in windows
                    ],
                }
                for asset, windows in data["windows"].items() if windows
            ],
        }

    def window(self, asset: str, pair: str, timeframe: str = "1d") -> dict[str, Any]:
        if timeframe == "1h":
            window = next((window for window in self._intraday_windows_for(asset) if window["pair"] == pair), None)
        elif timeframe == "1d":
            window = next((window for window in self._load()["windows"].get(asset, []) if window["pair"] == pair), None)
        else:
            raise W0Error("unsupported calendar timeframe")
        if window is None:
            raise W0Error("Нет полного календарного окна с локальными данными этого таймфрейма.")
        return window

    @staticmethod
    def _through(window: dict[str, Any], day: str) -> dict[str, Any]:
        if window.get("timeframe") == "1h":
            rows = [
                row for row in window["rows"]
                if (row.get("session_date", row["date"][:10]) <= day if len(day) == 10 else row["date"] <= day)
            ]
            if len(rows) < 2:
                raise W0Error("Недостаточно часовых наблюдений для проверки параметров")
            session_liquidity: dict[str, float] = {}
            for row in rows:
                session_liquidity[row["session_date"]] = float(row.get("liquidity_cumulative", 0.0))
            min_liquidity = min(session_liquidity.values()) if session_liquidity else 0.0
            tradable = bool(session_liquidity) and all(value >= window["liquidity_gate"] for value in session_liquidity.values())
            return {
                **window,
                "rows": rows,
                "last_date": rows[-1]["date"],
                "observations": len(rows),
                "min_liquidity_volume": min_liquidity,
                "min_spread_volume": min_liquidity if window["has_native_spread_volume"] else None,
                "tradable_every_day": tradable,
                "tradable_every_session": tradable,
                "max_pair_margin_rub": round(max(row["margin"] for row in rows), 2),
            }
        rows = [row for row in window["rows"] if row["date"] <= day]
        eligible = _positive_spread_volume_rows(rows)
        if len(eligible) < 2:
            raise W0Error("Недостаточно наблюдений для проверки параметров")
        return {**window, "rows": rows, "last_date": rows[-1]["date"],
                "min_spread_volume": min(row["min_volume"] for row in eligible),
                "max_pair_margin_rub": round(max(row["margin"] for row in rows), 2)}

    def check_training_window(self, asset, pair, quantity, settings):
        """Cheap preflight for a prefix, with the same source-specific volume gate as replay."""
        from .evaluation_periods import EvaluationSplit

        if settings.allow_failed_volume and settings.allow_failed_capital:
            return
        timeframe = getattr(settings, "timeframe", "1d")
        window = self.window(asset, pair, timeframe)
        if settings.validation_percent:
            if timeframe == "1h":
                observations = [row["date"] for row in window["rows"] if row.get("signal_eligible")]
            else:
                observations = [row["date"] for row in _positive_spread_volume_rows(window["rows"])]
            cutoff = EvaluationSplit.from_dates(observations, settings.validation_percent).training[-1]
            window = self._through(window, cutoff)
        if not settings.allow_failed_volume:
            if timeframe == "1h":
                volume_ok = window["tradable_every_session"]
            else:
                volume_ok = window["min_spread_volume"] >= MIN_DAILY_VOLUME
            if not volume_ok:
                raise W0Error("Не пройдено правило ликвидности на обучении.")
        if not settings.allow_failed_capital and not _stress_ok(window["max_pair_margin_rub"], quantity, settings.initial_capital_rub):
            raise W0Error("Не пройден стресс-тест капитала на обучении.")

    def proof(
        self,
        selection: dict[str, Any],
        *,
        through: str | None = None,
        initial_capital: float = INITIAL_CAPITAL,
        timeframe: str | None = None,
        metrics_only: bool = False,
        sizing_intents: bool = False,
        _cache: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = self._load()
        asset, pair = selection.get("asset"), selection.get("pair")
        timeframe = timeframe or selection.get("timeframe", "1d")
        strategy, fill_label = selection.get("strategy", "zscore_mean_reversion"), selection.get("fill", "next_open")
        quantity, slippage = selection.get("quantity", 1), selection.get("slippage_ticks", 1.0)
        if not isinstance(asset, str) or asset not in ASSETS or strategy not in STRATEGIES or fill_label not in FILL_LABELS:
            raise W0Error("unknown asset, strategy or fill rule")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 50:
            raise W0Error("contracts must be an integer between 1 and 50")
        through_cache = _cache.setdefault("through", {}) if _cache is not None else None
        if through_cache is not None and through in through_cache:
            window = through_cache[through]
        else:
            window = self.window(asset, pair, timeframe)
            if through is not None:
                window = self._through(window, through)
            if through_cache is not None:
                through_cache[through] = window
        delay, fill = next((d, f) for label, d, f in FILLS if label == fill_label)
        options = {"entry_z": 1.5, "exit_z": 0.5, **_zscore_options(asset)}
        params = selection.get("parameters", {})
        if not isinstance(params, dict) or set(params) - {"entry_z", "exit_z", "window", "threshold_pp"}:
            raise W0Error("unknown strategy parameter")
        for key in ("entry_z", "exit_z", "window"):
            if key in params:
                options[key] = params[key]
        threshold = float(params.get("threshold_pp", 1.0))
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
                   for value in (*options.values(), threshold)):
            raise W0Error("strategy parameters must be positive finite numbers")
        options["window"] = int(options["window"])
        profile = {**tariff_profile(float(slippage)), "tariff_group": ASSETS[asset]["group"]}
        z_cache = _cache.setdefault("z", {}) if _cache is not None else None
        z_cache_key = (through, fill, quantity)
        runner = strategy_runner(strategy, delay=delay, fill=fill, close_at_end=True, skip_dividend_windows_in_zscore=False,
                                 threshold=threshold, zscore_options=options, z_cache=z_cache, z_cache_key=z_cache_key)
        if timeframe == "1h":
            h1_cache = _cache.setdefault("h1", {}) if _cache is not None else None
            h1_key = (through, fill, quantity)
            if h1_cache is not None and h1_key in h1_cache:
                window, analysis_rows = h1_cache[h1_key]
            else:
                h1_rows = []
                is_open = fill == "open"
                gate = window["liquidity_gate"]
                for row in window["rows"]:
                    row = dict(row)
                    close_ok = bool(row.get("volume_eligible"))
                    if is_open:
                        fill_ok = (
                            row.get("liquidity_cumulative_before", 0.0) >= gate
                            and row.get("prior_bar_volume", 0.0) >= quantity
                        )
                    else:
                        fill_ok = close_ok and row.get("min_volume", 0.0) >= quantity
                    row["signal_eligible"] = close_ok
                    row["entry_eligible"] = bool(fill_ok)
                    row["exit_eligible"] = bool(fill_ok)
                    h1_rows.append(row)
                if sum(bool(row["signal_eligible"]) for row in h1_rows) < 2:
                    raise W0Error("fewer than two causally liquidity-eligible hourly observations")
                window = {**window, "rows": h1_rows}
                analysis_rows = h1_rows
                if h1_cache is not None:
                    h1_cache[h1_key] = (window, analysis_rows)
        else:
            # Zero-volume native spread rows remain journal-only on the daily path.
            elig_cache = _cache.setdefault("daily_eligible", {}) if _cache is not None else None
            if elig_cache is not None and through in elig_cache:
                analysis_rows = elig_cache[through]
            else:
                analysis_rows = _positive_spread_volume_rows(window["rows"])
                if len(analysis_rows) < 2:
                    raise W0Error("fewer than two positive-volume calendar-spread observations")
                if elig_cache is not None:
                    elig_cache[through] = analysis_rows
        per_pair = {pair: runner(analysis_rows)}
        native_order = window.get("native_spread_order", True)
        account = account_trades("proof", per_pair, data["benchmark"], profile,
                                 native_spread=native_order, quantity=quantity,
                                 benchmark_dates=data["benchmark_dates"])
        series_cache = _cache.setdefault("series", {}) if (_cache is not None and metrics_only) else None
        series_key = (through, fill, quantity) if timeframe == "1h" else (through,)
        payload = _proof_payload(
            window, account, asset, strategy, fill_label, options, threshold, quantity,
            float(slippage), data["terms"][asset], profile, initial_capital,
            metrics_only=metrics_only, sizing_intents=sizing_intents,
            series_cache=series_cache, series_key=series_key,
        )
        price_source = window.get("price_source", "native_daily_spread")
        payload["selection"].update({"timeframe": timeframe, "price_source": price_source})
        if metrics_only:
            payload["window"].update({
                "timeframe": timeframe,
                "price_source": price_source,
            })
            return payload
        payload["window"].update({
            "timeframe": timeframe,
            "price_source": price_source,
            "price_source_label": window.get("price_source_label", "Нативная дневная история календарного спреда"),
            "has_native_spread_volume": window.get("has_native_spread_volume", True),
            "observations": len(window["rows"]),
            "min_liquidity_volume": window.get("min_liquidity_volume", window.get("min_spread_volume")),
            "liquidity_source": window.get("liquidity_source", "native_daily_spread_volume"),
            "liquidity_gate": window.get("liquidity_gate", MIN_DAILY_VOLUME),
            "liquidity_gate_unit": window.get("liquidity_gate_unit", "контр./день"),
            "liquidity_volume_label": window.get("liquidity_volume_label", "минимальный дневной объём нативного спреда"),
            "liquidity_rule": window.get("liquidity_rule", f"Нативный spread-volume не менее {MIN_DAILY_VOLUME} контрактов в день."),
            "tradable_every_day": window.get("tradable_every_day", (window.get("min_spread_volume") or 0) >= MIN_DAILY_VOLUME),
            "tradable_every_session": window.get("tradable_every_session", window.get("tradable_every_day", True)),
            "min_liquidity_volume": window.get("min_liquidity_volume", window.get("min_spread_volume")),
            "min_spread_volume": window.get("min_spread_volume"),
            "native_fallback_reason": window.get("native_fallback_reason"),
            "session_count": window.get("session_count"),
        })
        if timeframe == "1h":
            payload["provenance"] = payload.get("provenance") or {}
            payload["provenance"].update(window.get("source_hashes", {}))
            source_note = (
                "Цена — нативная свеча H1 календарного spread-инструмента; для комиссий, маржи и RUONIA синхронно используются цены обеих ног."
                if price_source == "native_h1_spread"
                else "Цена — явно помеченный синтетический proxy far − near по точным общим timestamp H1 свечей ног; объём самого spread-инструмента не наблюдаем."
            )
            gate_note = window["liquidity_rule"]
            payload["notes"] = [
                source_note,
                gate_note,
                "Сигналы и исполнения используют только свечи 1h; дневные цены не подставляются. next_open использует открытие следующего доступного синхронизированного часового бара.",
                "RUONIA для каждого часового бара — последняя дневная запись строго до его торговой даты; дневной ряд не интерполируется и не размножается как отдельные часовые наблюдения.",
                "Стоимость пункта валютных фьючерсов берётся по последней доступной дневной FX-истории строго до торговой даты бара.",
                "Издержки native order пересекают один spread-book; для синтетического proxy считаются две отдельные ноги и проскальзывание обеих ног.",
                "Открытая на конец окна позиция — принудительная отметка, не исполнимая сделка; свечные OHLC/объёмы являются ценовыми прокси, не тиковыми сделками.",
            ]
        return payload

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


def _elapsed_days(start: str, end: str) -> float:
    if "T" in start or "T" in end:
        left = datetime.fromisoformat(str(start).replace("Z", ""))
        right = datetime.fromisoformat(str(end).replace("Z", ""))
        return round((right - left).total_seconds() / 86400, 4)
    return (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days


def _proof_payload(
    window,
    account,
    asset,
    strategy,
    fill_label,
    options,
    threshold,
    quantity,
    slippage,
    terms,
    profile,
    initial_capital=INITIAL_CAPITAL,
    *,
    metrics_only: bool = False,
    sizing_intents: bool = False,
    series_cache: dict | None = None,
    series_key: Any = None,
) -> dict[str, Any]:
    rows = window["rows"]
    tf = window.get("timeframe", "1d")
    if metrics_only:
        is_open = fill_label == "next_open"
        if series_cache is not None and series_key in series_cache:
            compact_series = series_cache[series_key]
        else:
            compact_series = [
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
                    "entry_eligible": r.get("entry_eligible", r.get("volume_eligible", r["min_volume"] > 0)),
                    "exit_eligible": r.get("exit_eligible", r.get("volume_eligible", r["min_volume"] > 0)),
                    "point_value_rub": r["mult"],
                }
                for r in rows
            ]
            if series_cache is not None:
                series_cache[series_key] = compact_series
        if sizing_intents:
            trades = [
                {
                    "number": number,
                    "side": "short_spread" if trade["direction"] < 0 else "long_spread",
                    "signal": trade["signal"],
                    "entry": {
                        "date": trade["entry_row"]["date"],
                        "spread_fill": trade["entry_row"]["spread_open_price"] if is_open else trade["entry_row"]["spread_price"],
                        "near_price": trade["entry_row"]["near_open_price"] if is_open else trade["entry_row"]["near_price"],
                        "far_price": trade["entry_row"]["far_open_price"] if is_open else trade["entry_row"]["far_price"],
                    },
                    "exit": {
                        "date": trade["exit_row"]["date"],
                        "spread_fill": trade["exit_row"]["spread_open_price"] if is_open else trade["exit_row"]["spread_price"],
                        "near_price": trade["exit_row"]["near_open_price"] if is_open else trade["exit_row"]["near_price"],
                        "far_price": trade["exit_row"]["far_open_price"] if is_open else trade["exit_row"]["far_price"],
                    },
                    "reason": trade["reason"],
                }
                for number, trade in enumerate(account["trade_list"], start=1)
            ]
            return {
                "schema_version": "calendar-proof.v1",
                "initial_capital_rub": initial_capital,
                "selection": {
                    "asset": asset,
                    "pair": window["pair"],
                    "spread_instrument": window["spread_instrument"],
                    "strategy": strategy,
                    "fill": fill_label,
                    "quantity": quantity,
                    "slippage_ticks": slippage,
                    "initial_capital_rub": initial_capital,
                    "parameters": {**options, "threshold_pp": threshold},
                },
                "window": {
                    "from": window["first_date"],
                    "to": window["last_date"],
                    "near_expiry": window["near_expiry"],
                    "near": window["near"],
                    "far": window["far"],
                    "tradable_every_day": window.get("tradable_every_day", (window.get("min_spread_volume") or 0) >= MIN_DAILY_VOLUME),
                    "passes_capital_screen": _stress_ok(window["max_pair_margin_rub"], quantity, initial_capital),
                    "max_pair_margin_rub": window["max_pair_margin_rub"],
                    "timeframe": tf,
                    "price_source": window.get("price_source", "native_daily_spread"),
                    "observations": len(rows),
                },
                "contract": {
                    "point_value_rub": terms["mult"],
                    "tick_size": terms["tick_size"],
                    "reference_contract": terms["reference_contract"],
                    "margin_rate": terms["margin_rate"],
                    "tariff_group": ASSETS[asset]["group"],
                    "taker_rate_percent": TAKER_RATE_PERCENT[ASSETS[asset]["group"]],
                    "broker_fee_rub": 0.0,
                },
                "series": compact_series,
                "trades": trades,
                "ledger": [],
                "summary": {},
            }
        trades = []
        for number, trade in enumerate(account["trade_list"], start=1):
            entry, exit_row, side = trade["entry_row"], trade["exit_row"], trade["direction"]
            entry_fee, exit_fee = trade["entry_costs"], trade["exit_costs"]
            trades.append({
                "number": number,
                "side": "short_spread" if side < 0 else "long_spread",
                "quantity": quantity,
                "signal": trade["signal"],
                "entry": {
                    "date": entry["date"],
                    "spread_fill": entry["spread_open_price"] if is_open else entry["spread_price"],
                    "costs": entry_fee,
                },
                "exit": {
                    "date": exit_row["date"],
                    "spread_fill": exit_row["spread_open_price"] if is_open else exit_row["spread_price"],
                    "costs": exit_fee,
                },
                "reason": trade["reason"],
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
            volume_eligible = row.get("volume_eligible", row.get("min_volume") is not None and row["min_volume"] > 0)
            entry_eligible = row.get("entry_eligible", volume_eligible)
            exit_eligible = row.get("exit_eligible", volume_eligible)
            if position is not None:
                exiting = by_exit.get(day)
                forced_end_mark = exiting is not None and exiting["reason"] == "end_of_window"
                if exiting is not None and (exit_eligible or forced_end_mark):
                    end = exiting["exit"]["spread_fill"]
                    move = position["sign"] * (end - position["mark"]) * point
                    daily += move
                    marked[position["number"]] += move
                    cost += sum(exiting["exit"]["costs"].values())
                    position = None
                elif volume_eligible:
                    move = position["sign"] * (row["spread_price"] - position["mark"]) * point
                    daily += move
                    marked[position["number"]] += move
                    position["mark"] = row["spread_price"]
            entering = by_entry.get(day) if entry_eligible else None
            if entering:
                sign = 1 if entering["side"] == "long_spread" else -1
                cost += sum(entering["entry"]["costs"].values())
                if volume_eligible:
                    move = sign * (row["spread_price"] - entering["entry"]["spread_fill"]) * point
                    daily += move
                    marked[entering["number"]] += move
                    mark = row["spread_price"]
                else:
                    mark = entering["entry"]["spread_fill"]
                position = {"sign": sign, "mark": mark, "number": entering["number"]}
            cumulative += daily - cost
            ledger.append({"date": day, "cumulative_net_rub": round(cumulative, 2)})
        for trade in trades:
            trade["gross_rub"] = round(marked[trade["number"]], 2)
            trade["net_rub"] = round(trade["gross_rub"] - trade["costs_rub"], 2)
            trade["excess_rub"] = round(trade["net_rub"] - trade["lqdt_on_margin_rub"], 2)
        gross = sum(t["gross_rub"] for t in trades)
        net = sum(t["net_rub"] for t in trades)
        return {
            "schema_version": "calendar-proof.v1",
            "initial_capital_rub": initial_capital,
            "selection": {
                "asset": asset,
                "pair": window["pair"],
                "spread_instrument": window["spread_instrument"],
                "strategy": strategy,
                "fill": fill_label,
                "quantity": quantity,
                "slippage_ticks": slippage,
                "initial_capital_rub": initial_capital,
                "parameters": {**options, "threshold_pp": threshold},
            },
            "window": {
                "from": window["first_date"],
                "to": window["last_date"],
                "near_expiry": window["near_expiry"],
                "near": window["near"],
                "far": window["far"],
                "tradable_every_day": window.get("tradable_every_day", (window.get("min_spread_volume") or 0) >= MIN_DAILY_VOLUME),
                "passes_capital_screen": _stress_ok(window["max_pair_margin_rub"], quantity, initial_capital),
                "max_pair_margin_rub": window["max_pair_margin_rub"],
                "timeframe": tf,
                "price_source": window.get("price_source", "native_daily_spread"),
                "observations": len(rows),
            },
            "contract": {
                "point_value_rub": terms["mult"],
                "tick_size": terms["tick_size"],
                "reference_contract": terms["reference_contract"],
                "margin_rate": terms["margin_rate"],
                "tariff_group": ASSETS[asset]["group"],
                "taker_rate_percent": TAKER_RATE_PERCENT[ASSETS[asset]["group"]],
                "broker_fee_rub": 0.0,
            },
            "series": compact_series,
            "trades": trades,
            "ledger": ledger,
            "summary": {
                "trades": len(trades),
                "gross_rub": round(gross, 2),
                "costs_rub": account["costs_rub"],
                "net_rub": round(net, 2),
                "lqdt_on_margin_rub": account["lqdt_on_pair_margin_while_open_rub"],
                "excess_over_lqdt_rub": round(net - account["lqdt_on_pair_margin_while_open_rub"], 2),
                "winners": sum(t["net_rub"] > 0 for t in trades),
            },
        }
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
                      "spread_volume": entry.get("spread_volume", entry["min_volume"]), "near_volume": entry["near_volume"], "far_volume": entry["far_volume"],
                      "costs": entry_fee},
            "exit": {"date": exit_row["date"], "spread_fill": exit_row["spread_open_price"] if fill_label == "next_open" else exit_row["spread_price"],
                     "spread_fill_rel": _rel_pct(exit_row["spread_open_price"] if fill_label == "next_open" else exit_row["spread_price"], exit_row["far_open_price"] if fill_label == "next_open" else exit_row["far_price"]),
                     "near_price": exit_row["near_open_price"] if fill_label == "next_open" else exit_row["near_price"],
                     "far_price": exit_row["far_open_price"] if fill_label == "next_open" else exit_row["far_price"],
                     "spread_volume": exit_row.get("spread_volume", exit_row["min_volume"]), "near_volume": exit_row["near_volume"], "far_volume": exit_row["far_volume"],
                     "costs": exit_fee},
            "reason": trade["reason"],
            "days_held": _elapsed_days(entry["date"], exit_row["date"]),
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
        volume_eligible = row.get("volume_eligible", row.get("min_volume") is not None and row["min_volume"] > 0)
        entry_eligible = row.get("entry_eligible", volume_eligible)
        exit_eligible = row.get("exit_eligible", volume_eligible)
        if position is not None:
            exiting = by_exit.get(day)
            forced_end_mark = exiting is not None and exiting["reason"] == "end_of_window"
            if exiting is not None and (exit_eligible or forced_end_mark):
                end = exiting["exit"]["spread_fill"]
                move = position["sign"] * (end - position["mark"]) * point
                daily += move
                marked[position["number"]] += move
                cost += sum(exiting["exit"]["costs"].values())
                position = None
            elif volume_eligible:
                move = position["sign"] * (row["spread_price"] - position["mark"]) * point
                daily += move
                marked[position["number"]] += move
                position["mark"] = row["spread_price"]
        entering = by_entry.get(day) if entry_eligible else None
        if entering:
            sign = 1 if entering["side"] == "long_spread" else -1
            cost += sum(entering["entry"]["costs"].values())
            if volume_eligible:
                move = sign * (row["spread_price"] - entering["entry"]["spread_fill"]) * point
                daily += move
                marked[entering["number"]] += move
                mark = row["spread_price"]
            else:
                mark = entering["entry"]["spread_fill"]
            position = {"sign": sign, "mark": mark, "number": entering["number"]}
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
        "initial_capital_rub": initial_capital,
        "selection": {"asset": asset, "pair": window["pair"], "spread_instrument": window["spread_instrument"], "strategy": strategy,
                      "fill": fill_label, "quantity": quantity, "slippage_ticks": slippage, "initial_capital_rub": initial_capital,
                      "parameters": {**options, "threshold_pp": threshold}},
        "window": {"from": window["first_date"], "to": window["last_date"], "near_expiry": window["near_expiry"],
                   "near": window["near"], "far": window["far"], "min_spread_volume": window.get("min_spread_volume"),
                   "tradable_every_day": window.get("tradable_every_day", (window.get("min_spread_volume") or 0) >= MIN_DAILY_VOLUME),
                   "min_daily_volume_rule": window.get("liquidity_gate", MIN_DAILY_VOLUME),
                   "excluded_volume_observations": sum(not r.get("volume_eligible", r.get("min_volume") is not None and r["min_volume"] > 0) for r in rows),
                   "passes_capital_screen": _stress_ok(window["max_pair_margin_rub"], quantity, initial_capital),
                   "max_pair_margin_rub": window["max_pair_margin_rub"],
                   "timeframe": window.get("timeframe", "1d"),
                   "price_source": window.get("price_source", "native_daily_spread"),
                   "has_native_spread_volume": window.get("has_native_spread_volume", True),
                   "observations": len(rows)},
        "contract": {"point_value_rub": terms["mult"], "tick_size": terms["tick_size"], "reference_contract": terms["reference_contract"],
                     "margin_rate": terms["margin_rate"],
                     "tariff_group": ASSETS[asset]["group"],
                     "taker_rate_percent": TAKER_RATE_PERCENT[ASSETS[asset]["group"]], "broker_fee_rub": 0.0},
        "series": [
            {"date": r["date"], "timestamp": r.get("timestamp", r["date"]),
             "session_date": r.get("session_date", r["date"][:10]),
             "benchmark_mark_date": r.get("benchmark_mark_date"),
             "timeframe": window.get("timeframe", "1d"),
             "near": r["near_price"], "far": r["far_price"], "near_open": r["near_open_price"],
             "far_open": r["far_open_price"], "spread": r["spread_price"], "spread_open": r["spread_open_price"],
             "spread_rel": _rel_pct(r["spread_price"], r["far_price"]),
             "spread_open_rel": _rel_pct(r["spread_open_price"], r["far_open_price"]),
             "spread_last_trade_rel": _rel_pct(r["spread_last_trade"], r["far_price"]),
             "spread_last_trade": r["spread_last_trade"], "spread_volume": r.get("spread_volume", r["min_volume"]),
             "volume_eligible": r.get("volume_eligible", r["min_volume"] > 0),
             "signal_eligible": r.get("signal_eligible", r.get("volume_eligible", r["min_volume"] > 0)),
             "entry_eligible": r.get("entry_eligible", r.get("volume_eligible", r["min_volume"] > 0)),
             "exit_eligible": r.get("exit_eligible", r.get("volume_eligible", r["min_volume"] > 0)),
             "spread_trades": r["spread_trades"],
             "near_volume": r["near_volume"], "far_volume": r["far_volume"], "days_to_near_expiry": r["days_to_near_expiry"],
             "dev_pp": r["dev_pp"],
             "ruonia_rate_pct": r.get("ruonia_rate") * 100 if r.get("ruonia_rate") is not None else None,
             "ruonia_rate_date": r.get("ruonia_rate_date"),
             "rate_price_per_pp": r["near_price"] * QUARTER_DAYS / 365 / 100,
             "margin_rub": round(r["margin"] * quantity, 2),
             "point_value_rub": r["mult"]}
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
            "Отметки — по расчётным ценам с дневной стоимостью пункта; издержки — опубликованный тариф биржи по обеим ногам плюс проскальзывание один раз на сторону.",
            "Для контрактов в USD и CNY стоимость пункта дрейфует вместе с валютой, поэтому день-в-день результат немного отличается от арифметики «вход–выход» (показано как дрейф стоимости пункта FX).",
            "Маржа — запасной коэффициент по сегодняшнему контракту; издержки LQDT — доходность бенчмарка на этой марже, пока позиция открыта.",
            "Спред показан в % от цены дальней ноги; абсолютные пункты остаются в CSV и во второй строке таблицы.",
        ],
    })
