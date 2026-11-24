"""Layer 4 loopback HTTP server, route handlers, and modular static web UI."""

from .server import ScreenerStore as ScreenerStore
from .server import make_handler as make_handler
from .server import serve as serve
from .static_assets import STATIC_DIR as STATIC_DIR
from .static_assets import VENDOR_DIR as VENDOR_DIR
from .strategy_registry import strategy_registry_payload as strategy_registry_payload

__all__ = [
    "STATIC_DIR",
    "VENDOR_DIR",
    "ScreenerStore",
    "make_handler",
    "serve",
    "strategy_registry_payload",
]
