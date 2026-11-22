"""H2/H3 perpetual SWAPRATE funding carry strategy subpackage."""

from .engine import *  # noqa: F403
from .plugin import PLUGIN, FundingCarryPlugin

__all__ = [
    "PLUGIN",
    "FundingCarryPlugin",
]
