"""Synchronized hourly (H1) calendar-window construction and session liquidity gating."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..calendar_ruonia.engine import implied_ruonia_deviation_pp, window_dividend_rub
from .contracts import (
    ASSETS,
    MIN_H1_CALENDAR_OBSERVATIONS,
    spread_id,
)
from .h1_candles import (
    asof_before,
    candle_timestamp,
    fetch_h1_candles,
    finite_num,
    read_h1_candles,
    select_h1_source_mode,
)


def _sum_native_session_volumes(
    session_days: list[str], native_common: list[str], source_rows: dict[str, dict[str, Any]]
) -> tuple[dict[str, float], set[str]]:
    vol: dict[str, float] = dict.fromkeys(session_days, 0.0)
    unknown: set[str] = set()
    for stamp in native_common:
        val = source_rows[stamp].get("volume")
        if val is None:
            unknown.add(stamp[:10])
        else:
            vol[stamp[:10]] += float(val)
    return vol, unknown


def _sum_proxy_session_volumes(
    session_days: list[str],
    common: list[str],
    near_rows: dict[str, dict[str, Any]],
    far_rows: dict[str, dict[str, Any]],
) -> tuple[dict[str, float], set[str]]:
    near_s: dict[str, float] = dict.fromkeys(session_days, 0.0)
    far_s: dict[str, float] = dict.fromkeys(session_days, 0.0)
    unknown: set[str] = set()
    for stamp in common:
        nv, fv = near_rows[stamp].get("volume"), far_rows[stamp].get("volume")
        if nv is None or fv is None:
            unknown.add(stamp[:10])
        else:
            near_s[stamp[:10]] += float(nv)
            far_s[stamp[:10]] += float(fv)
    return {day: min(near_s[day], far_s[day]) for day in session_days}, unknown


def _compute_h1_session_liquidity(
    is_native: bool,
    common: list[str],
    native_common: list[str],
    source_rows: dict[str, dict[str, Any]],
    near_rows: dict[str, dict[str, Any]],
    far_rows: dict[str, dict[str, Any]],
    gate: float,
) -> tuple[list[str], dict[str, float], float, bool]:
    session_days = sorted({stamp[:10] for stamp in common})
    session_volume, unknown = (
        _sum_native_session_volumes(session_days, native_common, source_rows)
        if is_native
        else _sum_proxy_session_volumes(session_days, common, near_rows, far_rows)
    )
    session_ok = {
        day: day not in unknown and session_volume.get(day, 0.0) >= gate for day in session_days
    }
    min_liq = min(session_volume.values()) if session_volume else 0.0
    return session_days, session_volume, min_liq, bool(session_ok) and all(session_ok.values())


@dataclass
class _H1BuildState:
    is_native: bool
    source_rows: dict[str, dict[str, Any]]
    near_rows: dict[str, dict[str, Any]]
    far_rows: dict[str, dict[str, Any]]
    session_volume: dict[str, float]
    gate: float
    terms: dict[str, Any]
    config: dict[str, Any]
    expiry_date: date
    dividend: float
    ruonia_dates: list[str]
    rates: dict[str, float]
    benchmark_dates: list[str]
    fx_name: str | None
    fx_dates: list[str]
    fx_values: dict[str, float]
    dte_cache: dict[str, int] = field(default_factory=dict)
    cum_native: dict[str, float] = field(default_factory=dict)
    cum_near: dict[str, float] = field(default_factory=dict)
    cum_far: dict[str, float] = field(default_factory=dict)
    prev_bar_vol: dict[str, float] = field(default_factory=dict)


def _step_h1_cum_liquidity(
    st: _H1BuildState,
    s_day: str,
    stamp: str,
    nr: dict[str, Any],
    fr: dict[str, Any],
    near_open_raw: float,
    far_open_raw: float,
) -> tuple[float, float, float, Any, float, float, float]:
    if st.is_native:
        nat = st.source_rows[stamp]
        sp_raw = float(nat["close"])
        sp_open_raw = (
            float(nat["open"]) if nat["open"] is not None else far_open_raw - near_open_raw
        )
        min_vol = max(0.0, float(nat.get("volume") or 0.0))
        before = st.cum_native.get(s_day, 0.0)
        current = before + min_vol
        prior_bar = st.prev_bar_vol.get(s_day, 0.0)
        st.cum_native[s_day] = current
        st.prev_bar_vol[s_day] = min_vol
        return sp_raw, sp_open_raw, min_vol, nat.get("volume"), before, current, prior_bar
    near_raw, far_raw = float(nr["close"]), float(fr["close"])
    sp_raw = far_raw - near_raw
    sp_open_raw = far_open_raw - near_open_raw
    nv_raw, fv_raw = nr.get("volume"), fr.get("volume")
    min_vol = (
        min(float(nv_raw), float(fv_raw)) if nv_raw is not None and fv_raw is not None else 0.0
    )
    nb, fb = st.cum_near.get(s_day, 0.0), st.cum_far.get(s_day, 0.0)
    before = min(nb, fb)
    nv_f = float(nv_raw) if nv_raw is not None else 0.0
    fv_f = float(fv_raw) if fv_raw is not None else 0.0
    st.cum_near[s_day], st.cum_far[s_day] = nb + nv_f, fb + fv_f
    current = min(st.cum_near[s_day], st.cum_far[s_day])
    prior_bar = st.prev_bar_vol.get(s_day, 0.0)
    st.prev_bar_vol[s_day] = min(nv_f, fv_f)
    return sp_raw, sp_open_raw, min_vol, None, before, current, prior_bar


def _build_single_h1_row(st: _H1BuildState, stamp: str) -> dict[str, Any] | None:
    s_day = stamp[:10]
    nr, fr = st.near_rows[stamp], st.far_rows[stamp]
    near_raw, far_raw = float(nr["close"]), float(fr["close"])
    near_open = float(nr["open"]) if nr["open"] is not None and nr["open"] > 0 else near_raw
    far_open = float(fr["open"]) if fr["open"] is not None and fr["open"] > 0 else far_raw
    fx_date, fx_factor = (
        asof_before(st.fx_dates, st.fx_values, s_day) if st.fx_name else (None, 1.0)
    )
    if fx_factor is None:
        return None
    mult = float(st.terms["mult"]) * fx_factor
    near_val, far_val = near_raw * mult, far_raw * mult
    b_idx = bisect_left(st.benchmark_dates, s_day) - 1
    if near_val <= 0 or far_val <= 0 or b_idx < 0:
        return None
    sp_raw, sp_open_raw, min_vol, sp_vol, before, current, prior_bar = _step_h1_cum_liquidity(
        st, s_day, stamp, nr, fr, near_open, far_open
    )
    rate_date, rate = asof_before(st.ruonia_dates, st.rates, s_day)
    sp_val, open_sp_val = sp_raw * mult, sp_open_raw * mult
    elig = current >= st.gate and min_vol > 0
    return {
        "date": stamp,
        "timestamp": stamp,
        "session_date": s_day,
        "benchmark_mark_date": st.benchmark_dates[b_idx],
        "spread": sp_val,
        "open_spread": open_sp_val,
        "spread_price": sp_raw,
        "spread_open_price": sp_open_raw,
        "near_close": near_val,
        "far_close": far_val,
        "near_price": near_raw,
        "far_price": far_raw,
        "near_open_price": near_open,
        "far_open_price": far_open,
        "near_open": near_open,
        "far_open": far_open,
        "near_volume": nr.get("volume"),
        "far_volume": fr.get("volume"),
        "spread_volume": sp_vol,
        "spread_trades": None,
        "spread_last_trade": None,
        "min_volume": min_vol,
        "session_liquidity_volume": st.session_volume.get(s_day, 0.0),
        "liquidity_cumulative_before": before,
        "liquidity_cumulative": current,
        "prior_bar_volume": prior_bar,
        "volume_eligible": elig,
        "signal_eligible": elig,
        "dev_pp": implied_ruonia_deviation_pp(
            sp_val,
            near_val,
            st.dividend,
            rate,
            convergence_supported=bool(st.config.get("convergence")),
        ),
        "ruonia_rate": rate,
        "ruonia_rate_date": rate_date,
        "fx_rate_date": fx_date,
        "days_to_near_expiry": st.dte_cache.setdefault(
            s_day, (st.expiry_date - date.fromisoformat(s_day)).days
        ),
        "dividend_in_window": st.dividend > 0,
        "margin": float(st.terms["margin_rate"]) * (near_val + far_val),
        "mult": mult,
        "near_price_raw": near_raw,
        "far_price_raw": far_raw,
        "spread_open": open_sp_val,
    }


def build_intraday_window(
    asset: str,
    daily_window: dict[str, Any],
    data: dict[str, Any],
    sources: tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, str | None],
) -> dict[str, Any] | None:
    near_source, far_source, native_source, native_error = sources
    if near_source is None or far_source is None:
        return None
    near, far = daily_window["near"], daily_window["far"]
    first_day, last_day = daily_window["first_date"][:10], daily_window["last_date"][:10]
    near_rows = {
        ts: row
        for ts, row in near_source["rows"].items()
        if first_day <= ts[:10] <= last_day and row["close"] > 0
    }
    far_rows = {
        ts: row
        for ts, row in far_source["rows"].items()
        if first_day <= ts[:10] <= last_day and row["close"] > 0
    }
    common = sorted(near_rows.keys() & far_rows.keys())
    if len(common) < 2:
        return None
    native_rows: dict[str, Any] = native_source["rows"] if native_source else {}
    native_common = [ts for ts in common if ts in native_rows]
    is_native, source_rows, fallback, gate, liq_src, liq_label, liq_rule = select_h1_source_mode(
        native_common, native_source, native_error
    )
    session_days, session_vol, min_liq, tradable = _compute_h1_session_liquidity(
        is_native, common, native_common, source_rows, near_rows, far_rows, gate
    )
    expiry = daily_window["near_expiry"]
    expiry_date = date.fromisoformat(expiry)
    config = ASSETS[asset]
    fx_name = config.get("fx")
    fx_vals: dict[str, float] = data["fx_factors"].get(fx_name, {}) if fx_name else {}
    ruonia_dates, rates = data["ruonia"]
    st = _H1BuildState(
        is_native=is_native,
        source_rows=source_rows,
        near_rows=near_rows,
        far_rows=far_rows,
        session_volume=session_vol,
        gate=gate,
        terms=data["terms"][asset],
        config=config,
        expiry_date=expiry_date,
        dividend=window_dividend_rub(asset, expiry_date),
        ruonia_dates=ruonia_dates,
        rates=rates,
        benchmark_dates=data["benchmark_dates"],
        fx_name=fx_name,
        fx_dates=sorted(fx_vals),
        fx_values=fx_vals,
    )
    obs_stamps = native_common if is_native else common
    rows = [r for ts in obs_stamps if (r := _build_single_h1_row(st, ts)) is not None]
    if len(rows) < MIN_H1_CALENDAR_OBSERVATIONS:
        return None
    rows.sort(key=lambda r: r["date"])
    source_hashes = {
        "near_candles_sha256": near_source["sha256"],
        "far_candles_sha256": far_source["sha256"],
    }
    if native_source:
        source_hashes["native_spread_candles_sha256"] = native_source["sha256"]
    price_source = "native_h1_spread" if is_native else "synthetic_leg_proxy"
    return {
        "pair": daily_window["pair"],
        "spread_instrument": spread_id(near, far),
        "near": near,
        "far": far,
        "first_date": rows[0]["date"],
        "last_date": rows[-1]["date"],
        "near_expiry": expiry,
        "rows": rows,
        "observations": len(rows),
        "min_spread_volume": min_liq if is_native else None,
        "min_liquidity_volume": min_liq,
        "max_pair_margin_rub": round(max(float(r["margin"]) for r in rows), 2),
        "timeframe": "1h",
        "price_source": price_source,
        "price_source_label": (
            "Нативные часовые свечи календарного спреда"
            if is_native
            else "Синтетический прокси far − near по синхронизированным часовым свечам ног"
        ),
        "native_fallback_reason": fallback,
        "has_native_spread_volume": is_native,
        "native_spread_order": is_native,
        "liquidity_source": liq_src,
        "liquidity_gate": gate,
        "liquidity_gate_unit": "контр./сессию",
        "liquidity_volume_label": liq_label,
        "liquidity_rule": liq_rule,
        "tradable_every_day": tradable,
        "tradable_every_session": tradable,
        "session_count": len(session_days),
        "source_hashes": source_hashes,
    }


__all__ = [
    "asof_before",
    "build_intraday_window",
    "candle_timestamp",
    "fetch_h1_candles",
    "finite_num",
    "read_h1_candles",
]
