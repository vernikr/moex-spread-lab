"""Native MOEX calendar-spread study over front windows, priced from official leg settlements."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from moex_spread_lab.core.settings import INITIAL_CAPITAL
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data import MarketDataStore
from moex_spread_lab.data.iss.history_eligibility import history_eligibility_signature
from moex_spread_lab.data.iss.iss_http import history_range_closed

from ..calendar_eval import FILLS, account_trades, evaluate_variants, read_benchmark
from .data_cache import (
    asset_eod_signature,
    asset_h1_signature,
    build_loaded_dict,
    is_offline,
    load_shared_metadata,
    resolve_intraday_till,
    run_calendar_data_proof,
    scan_cache_json_sigs,
    slice_window_through,
    stat_extras,
    validate_training_window,
)
from .h1_candles import read_h1_candles
from .h1_windows import (
    asof_before,
    build_intraday_window,
    candle_timestamp,
    finite_num,
)
from .loaders import cached_till_date, fetch_inputs
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
    build_windows,
    contract_chain,
    fx_factors,
    offline_loader,
    positive_spread_volume_rows,
    spread_id,
    stress_ok,
)
from .study import run_calendar_study as _run_calendar_study

# Legacy spellings the root backward-compatibility shim still re-exports; that shim is deleted by
# the cross-package issue, so this is the only surviving private alias pair.
_positive_spread_volume_rows = positive_spread_volume_rows
_stress_ok = stress_ok


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


def _reusable_history_snapshot(data: dict[str, Any]) -> bool:
    return not data["histories"] or history_range_closed(data["till"])


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

    def _eod_signature(self) -> tuple[Any, ...]:
        files, _ = scan_cache_json_sigs(self.cache)
        extras = stat_extras(self.root)
        freshness = history_eligibility_signature((self.cache,))
        return (tuple(files), tuple(extras), freshness)

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
        if (
            self._loaded is not None
            and _reusable_history_snapshot(self._loaded)
            and self._loaded_sig == self._eod_signature()
        ):
            return self._loaded
        meta = self._shared_metadata()
        sig = asset_eod_signature(self.cache, asset, meta["till"], meta["extras_sig"])
        if (
            asset in self._loaded_assets
            and _reusable_history_snapshot(self._loaded_assets[asset])
            and self._loaded_assets_sig.get(asset) == sig
        ):
            return self._loaded_assets[asset]
        offline = self.offline or is_offline(
            asset in self._asset_fetched_once, self._eod_fetched_once
        )
        histories = fetch_inputs(
            self.cache,
            (asset,),
            meta["till"],
            delay_seconds=0 if offline else 0.05,
            opener=offline_loader if offline else None,
        )
        self._asset_fetched_once.add(asset)
        sig = asset_eod_signature(self.cache, asset, meta["till"], meta["extras_sig"])
        data = build_loaded_dict(meta, histories, (asset,), fx_factors, build_windows)
        self._loaded_assets[asset] = data
        self._loaded_assets_sig[asset] = sig
        self._h1_windows.pop(asset, None)
        self._h1_windows_sig.pop(asset, None)
        return data

    def _load(self) -> dict[str, Any]:
        sig = self._eod_signature()
        if (
            self._loaded is not None
            and _reusable_history_snapshot(self._loaded)
            and self._loaded_sig == sig
        ):
            return self._loaded
        meta = self._shared_metadata()
        offline = self.offline or is_offline(self._eod_fetched_once)
        histories = fetch_inputs(
            self.cache,
            tuple(ASSETS),
            meta["till"],
            delay_seconds=0 if offline else 0.05,
            opener=offline_loader if offline else None,
        )
        self._eod_fetched_once = True
        self._loaded = build_loaded_dict(
            self._shared_metadata(), histories, tuple(ASSETS), fx_factors, build_windows
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

    def _read_h1_candles(
        self, secid: str, *, allow_fetch: bool = True
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Hourly candles through the sealed lake first, cache second, network only if allowed."""
        return read_h1_candles(
            self.root,
            self.bar_store,
            secid,
            self._h1_candle_cache,
            self._h1_fetch_attempted,
            allow_fetch=allow_fetch,
            offline=self.offline,
        )

    def _intraday_window(
        self,
        asset: str,
        daily_window: dict[str, Any],
        data: dict[str, Any],
        *,
        allow_fetch: bool = True,
    ) -> dict[str, Any] | None:
        """Proxy ``far - near`` hourly window, or ``None`` when a leg has no local candles."""
        near, far = daily_window["near"], daily_window["far"]
        near_src, _ = self._read_h1_candles(near, allow_fetch=allow_fetch)
        far_src, _ = self._read_h1_candles(far, allow_fetch=allow_fetch)
        if near_src is None or far_src is None:
            return None
        nat_src, nat_err = self._read_h1_candles(spread_id(near, far), allow_fetch=allow_fetch)
        return build_intraday_window(
            asset, daily_window, data, (near_src, far_src, nat_src, nat_err)
        )

    def _intraday_windows_for(
        self, asset: str, *, allow_fetch: bool = True
    ) -> list[dict[str, Any]]:
        """All H1 windows of one asset, cached by the EOD + H1 signature."""
        data = self._load_asset(asset) if asset in ASSETS else self._load()
        eod_sig = self._loaded_assets_sig.get(asset) or self._loaded_sig
        sig = asset_h1_signature(self.root, self.bar_store, asset, data, eod_sig)
        fkey = f"_fetched_{asset}"
        if (
            asset in self._h1_windows
            and self._h1_windows_sig.get(asset) == sig
            and (not allow_fetch or bool(self._h1_windows_sig.get(fkey)))
        ):
            return self._h1_windows[asset]
        wins = (
            [
                win
                for dw in data["windows"].get(asset, [])
                if (win := self._intraday_window(asset, dw, data, allow_fetch=allow_fetch))
                is not None
            ]
            if asset in ASSETS and asset in data["terms"]
            else []
        )
        self._h1_windows[asset] = wins
        self._h1_windows_sig[asset] = asset_h1_signature(
            self.root, self.bar_store, asset, data, eod_sig
        )
        if allow_fetch:
            self._h1_windows_sig[fkey] = (True,)
        return wins

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
    "_positive_spread_volume_rows",
    "_stress_ok",
    "account_trades",
    "build_windows",
    "cached_till_date",
    "contract_chain",
    "evaluate_variants",
    "fetch_inputs",
    "positive_spread_volume_rows",
    "read_benchmark",
    "run_calendar_study",
    "spread_id",
    "stress_ok",
]
