"""Layer 4 loopback HTTP server, route handlers, and modular static web UI."""

from .http_handler import STATIC_DIR as STATIC_DIR
from .http_handler import VENDOR_DIR as VENDOR_DIR
from .routes_proof import strategy_registry_payload as strategy_registry_payload
from .server import ScreenerStore as ScreenerStore
from .server import make_handler as make_handler
from .server import serve as serve

__all__ = [
    "STATIC_DIR",
    "VENDOR_DIR",
    "ScreenerStore",
    "make_handler",
    "serve",
    "strategy_registry_payload",
]
