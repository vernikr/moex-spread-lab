"""Layer 4 loopback HTTP server, route handlers, and modular static web UI."""

from __future__ import annotations

from .routes_proof import strategy_registry_payload
from .server import (
    STATIC_DIR,
    VENDOR_DIR,
    ScreenerError,
    ScreenerInputs,
    ScreenerStore,
    build_result,
    catalog,
    make_handler,
    serve,
)

__all__ = [
    "STATIC_DIR",
    "VENDOR_DIR",
    "ScreenerError",
    "ScreenerInputs",
    "ScreenerStore",
    "build_result",
    "catalog",
    "make_handler",
    "serve",
    "strategy_registry_payload",
]
