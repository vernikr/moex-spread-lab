"""H2/H3 perpetual SWAPRATE funding carry strategy subpackage."""

from .engine import *  # noqa: F401,F403
from .plugin import PLUGIN, FundingCarryPlugin

__all__ = [  # noqa: F405
    "PLUGIN",
    "FundingCarryPlugin",
]
