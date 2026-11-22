"""Native MOEX calendar-spread study over front windows, priced from official leg settlements."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from moex_spread_lab.core.settings import INITIAL_CAPITAL
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.lake.data_lake import MarketDataStore

from ..calendar_eval import FILLS, account_trades, evaluate_variants, read_benchmark
from . import adjacent
from .data_cache import (
    _is_offline,
    asset_eod_signature,
    build_loaded_dict,
    load_shared_metadata,
    resolve_intraday_till,
    run_calendar_data_proof,
    scan_cache_json_sigs,
    slice_window_through,
    stat_extras,
    validate_training_window,
)
from .h1_windows import (
    asof_before,
    candle_timestamp,
    finite_num,
)
from .loaders import cached_till_date, fetch_inputs
from .proof_ledger import elapsed_days as _elapsed_days
from .proof_payload import _clean, _proof_payload, _rel_pct
from .proof_runner import build_1d_catalog, build_h1_catalog
from .study import (
    ASSETS,
    CROSS_EXPIRY_PAIRS,
    EXIT_BUFFER_DAYS,
    FILL_LABELS,
    FX_SOURCE,
    HISTORY_FIELDS,
    HISTORY_FROM,
    MIN_H1_CALENDAR_OBSERVATIONS,
    MIN_H1_NATIVE_SESSION_VOLUME,
    MIN_H1_PROXY_LEG_SESSION_VOLUME,
    MONTH_CODES,
    QUARTER_CODES,
    STRATEGIES,
    SYNTHETIC_TRIANGLE_PAIRS,
    ZSCORE_OPTIONS,
    _calendar_strategy_runner,
    _fx_factors,
    _offline,
    _positive_spread_volume_rows,
    _ruonia,
    _span_years,
    _stress_ok,
    _terms,
    _zscore_options,
    build_windows,
    contract_chain,
    spread_id,
)
from .study import run_calendar_study as _run_calendar_study


def run_calendar_study(
    cache_dir: Path,
    benchmark_path: Path,
    benchmark_cache_dir: Path,
    contracts_path: Path,
    *,
    assets: tuple[str, ...] = tuple(ASSETS),
    slippage_ticks: float = 1.0,
    till_date: str,
) -> dict[str, Any]:
    return _run_calendar_study(
        cache_dir,
        benchmark_path,
        benchmark_cache_dir,
        contracts_path,
        assets=assets,
        slippage_ticks=slippage_ticks,
        till_date=till_date,
        fetch_fn=fetch_inputs,
    )


class CalendarData:
    """Offline view of cached daily and hourly calendar-spread inputs.

    Reads cached EOD history, H1 candles and benchmark files; ``offline=False`` (explicit intake
    commands only) is the sole way to allow MOEX ISS connections.
    """

    def __init__(self, root: Path, *, offline: bool = True) -> None:
        self.root = root.resolve()
        self.offline = offline
        self.cache = self.root / "data/spread-history-cache"
        self._loaded: dict[str, Any] | None = None
        self._loaded_sig: tuple[Any, ...] | None = None
        self.bar_store = MarketDataStore(self.root)
        self._h1_candle_cache: dict[
            str, tuple[tuple[Any, ...], tuple[dict[str, Any] | None, str | None]]
        ] = {}
        self._h1_fetch_attempted: set[str] = set()
        self._h1_windows: dict[str, list[dict[str, Any]]] = {}
        self._h1_windows_sig: dict[str, tuple[Any, ...]] = {}
        self._loaded_assets: dict[str, dict[str, Any]] = {}
        self._loaded_assets_sig: dict[str, tuple[Any, ...]] = {}
        self._shared_meta: dict[str, Any] | None = None
        self._shared_meta_sig: tuple[Any, ...] | None = None
        self._eod_fetched_once: bool = False
        self._asset_fetched_once: set[str] = set()
        self._last_eod_sig_time: float = 0.0
        self._last_eod_dir_sig: tuple[int, int] | None = None

    def _eod_signature(self) -> tuple[Any, ...]:
        if (
            "PYTEST_CURRENT_TEST" not in os.environ
            and self._loaded_sig is not None
            and time.monotonic() - self._last_eod_sig_time < 0.25
        ):
            try:
                dst = self.cache.stat()
                if (dst.st_mtime_ns, dst.st_size) == self._last_eod_dir_sig:
                    return self._loaded_sig
            except OSError:
                pass
        files, self._last_eod_dir_sig = scan_cache_json_sigs(self.cache)
        extras = stat_extras(self.root)
        self._last_eod_sig_time = time.monotonic()
        return (tuple(files), tuple(extras))

    def _shared_metadata(self) -> dict[str, Any]:
        dir_sig = None
        if self.cache.is_dir():
            try:
                dst = self.cache.stat()
                dir_sig = (dst.st_mtime_ns, dst.st_size)
            except OSError:
                pass
        sig = (tuple(stat_extras(self.root)), dir_sig)
        if self._shared_meta is None or self._shared_meta_sig != sig:
            self._shared_meta = load_shared_metadata(self.root, self.cache, read_benchmark)
            self._shared_meta_sig = sig
        return self._shared_meta

    def _load_asset(self, asset: str) -> dict[str, Any]:
        if asset not in ASSETS:
            return self._load()
        if self._loaded is not None and self._loaded_sig == self._eod_signature():
            return self._loaded
        meta = self._shared_metadata()
        sig = asset_eod_signature(self.cache, asset, meta["till"], meta["extras_sig"])
        if asset in self._loaded_assets and self._loaded_assets_sig.get(asset) == sig:
            return self._loaded_assets[asset]
        offline = self.offline or _is_offline(
            asset in self._asset_fetched_once, self._eod_fetched_once
        )
        histories = fetch_inputs(
            self.cache,
            (asset,),
            meta["till"],
            delay_seconds=0 if offline else 0.05,
            opener=_offline if offline else None,
        )
        self._asset_fetched_once.add(asset)
        sig = asset_eod_signature(self.cache, asset, meta["till"], meta["extras_sig"])
        data = build_loaded_dict(meta, histories, (asset,), _fx_factors, build_windows)
        self._loaded_assets[asset] = data
        self._loaded_assets_sig[asset] = sig
        self._h1_windows.pop(asset, None)
        self._h1_windows_sig.pop(asset, None)
        return data

    def _load(self) -> dict[str, Any]:
        sig = self._eod_signature()
        if self._loaded is not None and self._loaded_sig == sig:
            return self._loaded
        meta = self._shared_metadata()
        offline = self.offline or _is_offline(self._eod_fetched_once)
        histories = fetch_inputs(
            self.cache,
            tuple(ASSETS),
            meta["till"],
            delay_seconds=0 if offline else 0.05,
            opener=_offline if offline else None,
        )
        self._eod_fetched_once = True
        self._loaded = build_loaded_dict(
            self._shared_metadata(), histories, tuple(ASSETS), _fx_factors, build_windows
        )
        self._loaded_sig = self._eod_signature()
        self._loaded_assets.clear()
        self._loaded_assets_sig.clear()
        self._h1_windows.clear()
        self._h1_windows_sig.clear()
        return self._loaded

    _candle_timestamp = staticmethod(candle_timestamp)
    _number = staticmethod(finite_num)
    _asof_before = staticmethod(asof_before)
    _through = staticmethod(slice_window_through)

    def _fetch_h1_candles(self, secid: str) -> tuple[dict[str, Any] | None, str | None]:
        return adjacent.fetch_h1(self.root, secid, self)

    def _read_h1_candles(
        self, secid: str, *, allow_fetch: bool = True
    ) -> tuple[dict[str, Any] | None, str | None]:
        return adjacent.read_h1(self, secid, allow_fetch)

    def _intraday_window(
        self,
        asset: str,
        daily_window: dict[str, Any],
        data: dict[str, Any],
        *,
        allow_fetch: bool = True,
    ) -> dict[str, Any] | None:
        return adjacent.intraday_window(self, asset, daily_window, data, allow_fetch)

    def _intraday_windows_for(
        self, asset: str, *, allow_fetch: bool = True
    ) -> list[dict[str, Any]]:
        return adjacent.intraday_windows_for(self, asset, allow_fetch)

    def catalog(
        self, *, initial_capital: float = INITIAL_CAPITAL, timeframe: str = "1d"
    ) -> dict[str, Any]:
        if timeframe == "1h":
            self._load()
            by_asset = {a: self._intraday_windows_for(a, allow_fetch=False) for a in ASSETS}
            all_wins = [w for wins in by_asset.values() for w in wins]
            return build_h1_catalog(
                by_asset, resolve_intraday_till(self.root, all_wins), initial_capital
            )
        if timeframe != "1d":
            raise W0Error("unsupported calendar timeframe")
        return build_1d_catalog(self._load(), initial_capital)

    def window(self, asset: str, pair: str, timeframe: str = "1d") -> dict[str, Any]:
        if timeframe == "1h":
            window = next((w for w in self._intraday_windows_for(asset) if w["pair"] == pair), None)
        elif timeframe == "1d":
            data = self._load_asset(asset) if asset in ASSETS else self._load()
            window = next((w for w in data["windows"].get(asset, []) if w["pair"] == pair), None)
        else:
            raise W0Error("unsupported calendar timeframe")
        if window is None:
            raise W0Error("Нет полного календарного окна с локальными данными этого таймфрейма.")
        return window

    def check_training_window(self, asset: str, pair: str, quantity: int, settings: Any) -> None:
        validate_training_window(self.window, asset, pair, quantity, settings)

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
        return run_calendar_data_proof(
            self,
            selection,
            {
                "through": through,
                "initial_capital": initial_capital,
                "timeframe": timeframe,
                "metrics_only": metrics_only,
                "sizing_intents": sizing_intents,
                "cache": _cache,
            },
        )


__all__ = [
    "ASSETS",
    "CROSS_EXPIRY_PAIRS",
    "EXIT_BUFFER_DAYS",
    "FILLS",
    "FILL_LABELS",
    "FX_SOURCE",
    "HISTORY_FIELDS",
    "HISTORY_FROM",
    "MIN_H1_CALENDAR_OBSERVATIONS",
    "MIN_H1_NATIVE_SESSION_VOLUME",
    "MIN_H1_PROXY_LEG_SESSION_VOLUME",
    "MONTH_CODES",
    "QUARTER_CODES",
    "STRATEGIES",
    "SYNTHETIC_TRIANGLE_PAIRS",
    "ZSCORE_OPTIONS",
    "CalendarData",
    "_calendar_strategy_runner",
    "_clean",
    "_elapsed_days",
    "_fx_factors",
    "_offline",
    "_positive_spread_volume_rows",
    "_proof_payload",
    "_rel_pct",
    "_ruonia",
    "_span_years",
    "_stress_ok",
    "_terms",
    "_zscore_options",
    "account_trades",
    "build_windows",
    "cached_till_date",
    "contract_chain",
    "evaluate_variants",
    "fetch_inputs",
    "read_benchmark",
    "run_calendar_study",
    "spread_id",
]
