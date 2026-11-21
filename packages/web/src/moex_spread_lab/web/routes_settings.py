"""HTTP route handlers for global research settings and timeframe metadata."""

from __future__ import annotations

from typing import Any

from moex_spread_lab.core.settings import read_settings, save_settings
from moex_spread_lab.core.timeframes import timeframe_options


def handle_get_settings(path: str, store: Any) -> tuple[bool, Any]:
    """Dispatch GET settings/timeframes routes. Returns ``(handled, payload)``."""
    if path == "/api/settings":
        return True, read_settings(store.inputs.root)
    if path == "/api/timeframes":
        return True, timeframe_options()
    return False, None


def handle_post_settings(path: str, selection: Any, store: Any) -> tuple[bool, Any]:
    """Dispatch POST ``/api/settings``. Returns ``(handled, payload)``."""
    if path == "/api/settings":
        with store.lock:
            return True, save_settings(store.inputs.root, selection)
    return False, None
