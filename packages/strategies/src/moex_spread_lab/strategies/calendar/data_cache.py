"""Filesystem signature and window-prefix slicing helpers for ``CalendarData``."""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Any

from moex_spread_lab.core.contracts import plugin_proof_adapter
from moex_spread_lab.core.settings import MIN_DAILY_VOLUME, EvaluationSplit
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.iss.history_eligibility import history_page_is_eligible
from moex_spread_lab.data.iss.iss_http import HISTORY_PAGE_SIZE

from ..base import calendar_proof_plugin_ids, discover_strategies
from .contracts import (
    ASSETS,
    FILL_LABELS,
    FX_SOURCE,
    HISTORY_FROM,
    STRATEGIES,
    contract_chain,
    positive_spread_volume_rows,
    ruonia,
    spread_id,
    stress_ok,
    terms,
)
from .proof_runner import execute_calendar_proof
from .study import cached_till_date


def stat_extras(root: Path) -> list[tuple[str, int, int] | None]:
    extras: list[tuple[str, int, int] | None] = []
    for rel in (
        "data/w1/current-intake.json",
        "data/benchmark-cache/CBR_RUONIA.json",
        "data/benchmark/lqdt-lineage.json",
    ):
        p = root / rel
        try:
            st = p.stat()
            extras.append((rel, st.st_mtime_ns, st.st_size))
        except OSError:
            extras.append(None)
    return extras


def scan_cache_json_sigs(
    cache_dir: Path,
) -> tuple[list[tuple[str, int, int, int, int]], tuple[int, int] | None]:
    files: list[tuple[str, int, int, int, int]] = []
    dir_sig: tuple[int, int] | None = None
    if not cache_dir.is_dir():
        return files, dir_sig
    try:
        dst = cache_dir.stat()
        dir_sig = (dst.st_mtime_ns, dst.st_size)
    except OSError:
        dir_sig = None
    for entry in os.scandir(cache_dir):
        if entry.name.endswith(".json"):
            try:
                st = entry.stat()
                files.append((entry.name, st.st_mtime_ns, st.st_size, st.st_ctime_ns, st.st_ino))
            except OSError:
                pass
    files.sort()
    return files, dir_sig


def asset_eod_signature(
    cache_dir: Path, asset: str, till: str, extras_sig: tuple[Any, ...]
) -> tuple[Any, ...]:
    needed = {asset}
    fx = ASSETS.get(asset, {}).get("fx")
    if fx:
        needed.add(FX_SOURCE[fx])
    secids: list[str] = []
    for a in sorted(needed):
        chain = contract_chain(a)
        secids.extend(chain)
        secids.extend(spread_id(n, f) for n, f in itertools.pairwise(chain))
    file_sigs: list[tuple[str, int | None, int | None, int | None, int | None, bool]] = []
    for secid in secids:
        idx = 0
        while True:
            p = cache_dir / f"{secid}-{HISTORY_FROM}-{till}-{idx}.json"
            try:
                st = p.stat()
                file_sigs.append(
                    (
                        p.name,
                        st.st_mtime_ns,
                        st.st_size,
                        st.st_ctime_ns,
                        st.st_ino,
                        history_page_is_eligible(p, till),
                    )
                )
                idx += HISTORY_PAGE_SIZE
            except OSError:
                if idx == 0:
                    file_sigs.append((p.name, None, None, None, None, False))
                break
    return (extras_sig, till, tuple(file_sigs))


def asset_h1_signature(
    root: Path, bar_store: Any, asset: str, data: dict[str, Any], eod_sig: Any
) -> tuple[Any, ...]:
    secids: set[str] = set()
    for dw in data["windows"].get(asset, []):
        secids.update((dw["near"], dw["far"], spread_id(dw["near"], dw["far"])))
    seal_sig = bar_store.sealed_h1._snapshot if bar_store.sealed_h1.payload() is not None else None
    file_sigs: list[tuple[Any, ...]] = []
    for secid in sorted(secids):
        for candidate in (
            bar_store.select_h1(secid),
            root / "data/w1/candles/1h" / f"{secid}_1h.json",
        ):
            try:
                st = candidate.stat()
                file_sigs.append((str(candidate), st.st_mtime_ns, st.st_size))
            except OSError:
                file_sigs.append((str(candidate), None, None))
    return (eod_sig, seal_sig, tuple(file_sigs))


def resolve_intraday_till(root: Path, windows: list[dict[str, Any]]) -> str | None:
    summary_path = root / "data/w1/candles/summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("interval") == 60 and isinstance(summary.get("till"), str):
            return summary["till"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        pass
    last = max((window["last_date"] for window in windows), default=None)
    return last[:10] if last else None


def slice_window_through(window: dict[str, Any], day: str) -> dict[str, Any]:
    if window.get("timeframe") == "1h":
        rows = [
            row
            for row in window["rows"]
            if (
                row.get("session_date", row["date"][:10]) <= day
                if len(day) == 10
                else row["date"] <= day
            )
        ]
        if len(rows) < 2:
            raise W0Error("Недостаточно часовых наблюдений для проверки параметров")
        session_liq = {
            row["session_date"]: float(row.get("liquidity_cumulative", 0.0)) for row in rows
        }
        min_liq = min(session_liq.values()) if session_liq else 0.0
        tradable = bool(session_liq) and all(
            v >= window["liquidity_gate"] for v in session_liq.values()
        )
        return {
            **window,
            "rows": rows,
            "last_date": rows[-1]["date"],
            "observations": len(rows),
            "min_liquidity_volume": min_liq,
            "min_spread_volume": min_liq if window["has_native_spread_volume"] else None,
            "tradable_every_day": tradable,
            "tradable_every_session": tradable,
            "max_pair_margin_rub": round(max(float(r["margin"]) for r in rows), 2),
        }
    rows = [row for row in window["rows"] if row["date"] <= day]
    eligible = positive_spread_volume_rows(rows)
    if len(eligible) < 2:
        raise W0Error("Недостаточно наблюдений для проверки параметров")
    return {
        **window,
        "rows": rows,
        "last_date": rows[-1]["date"],
        "min_spread_volume": min(float(r["min_volume"]) for r in eligible),
        "max_pair_margin_rub": round(max(float(r["margin"]) for r in rows), 2),
    }


def validate_training_window(
    window_fn: Any, asset: str, pair: str, quantity: int, settings: Any
) -> None:
    if settings.allow_failed_volume and settings.allow_failed_capital:
        return
    timeframe = getattr(settings, "timeframe", "1d")
    window = window_fn(asset, pair, timeframe)
    if settings.validation_percent:
        observations = (
            [r["date"] for r in window["rows"] if r.get("signal_eligible")]
            if timeframe == "1h"
            else [r["date"] for r in positive_spread_volume_rows(window["rows"])]
        )
        cutoff = EvaluationSplit.from_dates(observations, settings.validation_percent).training[-1]
        window = slice_window_through(window, cutoff)
    if not settings.allow_failed_volume:
        vol_ok = (
            window["tradable_every_session"]
            if timeframe == "1h"
            else window["min_spread_volume"] >= MIN_DAILY_VOLUME
        )
        if not vol_ok:
            raise W0Error("Не пройдено правило ликвидности на обучении.")
    if not settings.allow_failed_capital and not stress_ok(
        window["max_pair_margin_rub"], quantity, settings.initial_capital_rub
    ):
        raise W0Error("Не пройден стресс-тест капитала на обучении.")


def _validate_proof_selection(
    cache_dir: Path, selection: dict[str, Any]
) -> tuple[str, str, int, bool, Any]:
    asset = selection.get("asset")
    strategy = str(selection.get("strategy", "zscore_mean_reversion"))
    fill_label = str(selection.get("fill", "next_open"))
    quantity = selection.get("quantity", 1)
    registry = discover_strategies()
    # External plugins opt in through the narrow calendar adapter set; built-in calendar
    # strategies join the same pipeline once their plugin declares the adapter contract.
    generic = strategy in calendar_proof_plugin_ids(registry) or (
        strategy in STRATEGIES and plugin_proof_adapter(registry.get(strategy)) is not None
    )
    valid_spec = (
        isinstance(asset, str)
        and asset in ASSETS
        and (strategy in STRATEGIES or generic)
        and fill_label in FILL_LABELS
    )
    if not valid_spec:
        if not cached_till_date(cache_dir):
            raise W0Error("calendar history is not cached; run calendar-study first")
        raise W0Error("unknown asset, strategy or fill rule")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 50:
        raise W0Error("contracts must be an integer between 1 and 50")
    assert isinstance(asset, str)
    return asset, strategy, quantity, generic, registry


def _resolve_proof_data_and_window(
    cal: Any,
    asset: str,
    pair: str,
    tf: str,
    through: str | None,
    cache: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    loaded_by_asset = cache.setdefault("_loaded_data", {}) if cache is not None else None
    if loaded_by_asset is not None and asset in loaded_by_asset:
        data = loaded_by_asset[asset]
    else:
        data = cal._load_asset(asset)
        if loaded_by_asset is not None:
            loaded_by_asset[asset] = data
    through_cache = cache.setdefault("through", {}) if cache is not None else None
    win_key = (asset, pair, tf, through)
    if through_cache is not None and win_key in through_cache:
        return data, through_cache[win_key]
    window = cal.window(asset, pair, tf)
    if through is not None:
        window = cal._through(window, through)
    if through_cache is not None:
        through_cache[win_key] = window
    return data, window


def run_calendar_data_proof(
    cal: Any, selection: dict[str, Any], opts: dict[str, Any]
) -> dict[str, Any]:
    asset, strategy, quantity, generic, registry = _validate_proof_selection(cal.cache, selection)
    pair = str(selection.get("pair"))
    tf = opts["timeframe"] or str(selection.get("timeframe", "1d"))
    through, cache = opts["through"], opts["cache"]
    data, window = _resolve_proof_data_and_window(cal, asset, pair, tf, through, cache)
    return execute_calendar_proof(
        data,
        window,
        {
            "asset": asset,
            "pair": pair,
            "timeframe": tf,
            "strategy": strategy,
            "fill_label": str(selection.get("fill", "next_open")),
            "quantity": quantity,
            "slippage": float(selection.get("slippage_ticks", 1.0)),
            "raw_params": selection.get("parameters", {}),
            "generic": generic,
            "registry": registry,
            "through": through,
            "initial_capital": opts["initial_capital"],
            "metrics_only": opts["metrics_only"],
            "sizing_intents": opts["sizing_intents"],
            "cache": cache,
        },
    )


def is_offline(*flags: bool) -> bool:
    return (
        os.environ.get("MOEX_OFFLINE_ONLY") == "1"
        or "PYTEST_CURRENT_TEST" in os.environ
        or any(flags)
    )


def build_loaded_dict(
    meta: dict[str, Any],
    histories: dict[str, Any],
    assets: tuple[str, ...],
    fx_fn: Any,
    win_fn: Any,
) -> dict[str, Any]:
    factors = fx_fn(histories)
    windows = {
        a: win_fn(histories, a, meta["terms"][a], meta["ruonia"], factors)
        for a in assets
        if a in meta["terms"] and any(s in histories for s in contract_chain(a))
    }
    return {
        "histories": histories,
        "windows": windows,
        "terms": meta["terms"],
        "till": meta["till"],
        "ruonia": meta["ruonia"],
        "fx_factors": factors,
        "benchmark": meta["benchmark"],
        "benchmark_dates": meta["benchmark_dates"],
    }


def load_shared_metadata(root: Path, cache: Path, read_benchmark_fn: Any) -> dict[str, Any]:
    extras = stat_extras(root)
    till = cached_till_date(cache)
    if till is None:
        raise W0Error("calendar history is not cached; run calendar-study first")
    contract_terms = terms(root / "data/w1/current-intake.json", tuple(ASSETS))
    ruonia_rates = ruonia(root / "data/benchmark-cache/CBR_RUONIA.json")
    benchmark = read_benchmark_fn(root / "data/benchmark/lqdt-lineage.json")
    return {
        "till": till,
        "terms": contract_terms,
        "ruonia": ruonia_rates,
        "benchmark": benchmark,
        "benchmark_dates": sorted(benchmark),
        "extras_sig": tuple(extras),
    }
