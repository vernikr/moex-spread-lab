"""Layer 0 (`msl-core`): canonical hashing (`w0`), settings, timeframes,
universe IDs, strategy contracts, derived cache, and the optional C99 `_fastcore`."""

from . import contracts, settings, timeframes, universe, w0
from ._fastcore_loader import load_fastcore

__all__ = [
    "contracts",
    "load_fastcore",
    "settings",
    "timeframes",
    "universe",
    "w0",
]
