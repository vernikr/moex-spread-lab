"""Offline futures-pair screener domain logic, input reader, and catalog builder."""

from .pair_screener import *  # noqa: F403

# Explicit aliased re-exports: the screen contract stays part of this package
# facade while whole-package strict typing sees the names as deliberately
# re-exported instead of unused imports. Import order still lets these names
# shadow anything of the same name in the star import above.
from .screener import SCREEN_SCHEMA as SCREEN_SCHEMA
from .screener import ScreenError as ScreenError
from .screener import screen_pairs as screen_pairs
