"""Adjacent-window (H1) helpers for the offline ``CalendarData`` view."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import ASSETS, spread_id
from .data_cache import asset_h1_signature
from .h1_candles import fetch_h1_candles, read_h1_candles
from .h1_windows import build_intraday_window


def fetch_h1(root: Path, secid: str, view: Any) -> tuple[dict[str, Any] | None, str | None]:
    """H1 candles for ``secid`` with the view's offline contract."""
    return fetch_h1_candles(
        root, secid, view._h1_candle_cache, view._h1_fetch_attempted, offline=view.offline
    )


def read_h1(
    view: Any, secid: str, allow_fetch: bool = True
) -> tuple[dict[str, Any] | None, str | None]:
    """Hourly candles through the sealed lake first, cache second, network only if allowed."""
    return read_h1_candles(
        view.root,
        view.bar_store,
        secid,
        view._h1_candle_cache,
        view._h1_fetch_attempted,
        allow_fetch=allow_fetch,
        offline=view.offline,
    )


def intraday_window(
    view: Any,
    asset: str,
    daily_window: dict[str, Any],
    data: dict[str, Any],
    allow_fetch: bool = True,
) -> dict[str, Any] | None:
    """Proxy ``far - near`` hourly window, or ``None`` when a leg has no local candles."""
    near, far = daily_window["near"], daily_window["far"]
    near_src, _ = read_h1(view, near, allow_fetch)
    far_src, _ = read_h1(view, far, allow_fetch)
    if near_src is None or far_src is None:
        return None
    nat_src, nat_err = read_h1(view, spread_id(near, far), allow_fetch)
    return build_intraday_window(asset, daily_window, data, (near_src, far_src, nat_src, nat_err))


def intraday_windows_for(view: Any, asset: str, allow_fetch: bool = True) -> list[dict[str, Any]]:
    """All H1 windows of one asset, cached by the EOD + H1 signature."""
    data = view._load_asset(asset) if asset in ASSETS else view._load()
    eod_sig = view._loaded_assets_sig.get(asset) or view._loaded_sig
    sig = asset_h1_signature(view.root, view.bar_store, asset, data, eod_sig)
    fkey = f"_fetched_{asset}"
    if (
        asset in view._h1_windows
        and view._h1_windows_sig.get(asset) == sig
        and (not allow_fetch or bool(view._h1_windows_sig.get(fkey)))
    ):
        return view._h1_windows[asset]
    wins = (
        [
            win
            for dw in data["windows"].get(asset, [])
            if (win := intraday_window(view, asset, dw, data, allow_fetch)) is not None
        ]
        if asset in ASSETS and asset in data["terms"]
        else []
    )
    view._h1_windows[asset] = wins
    view._h1_windows_sig[asset] = asset_h1_signature(
        view.root, view.bar_store, asset, data, eod_sig
    )
    if allow_fetch:
        view._h1_windows_sig[fkey] = (True,)
    return wins
