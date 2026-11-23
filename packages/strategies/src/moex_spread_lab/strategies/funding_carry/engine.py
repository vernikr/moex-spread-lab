"""Unified engine exports for H2/H3 perpetual SWAPRATE funding carry."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from . import funding_recheck as _funding_recheck
from .carry import (
    CARRY_SCHEMA,
    FUNDING_REGIMES,
    STRATEGY_SCHEMA,
    CarryError,
    CarryStrategyError,
    build_carry_strategy,
    build_carry_study,
    fetch_history,
)
from .funding_screen import (
    FUNDING_SCREEN_PAIRS,
    FUNDING_SCREEN_SCHEMA,
    FundingScreenError,
    screen_funding_pairs,
)
from .proof_funding import (
    DSVD_NOTICE,
    FX_NEW_REGIME,
    FX_PERPETUALS,
    LAUNCH_PERPETUALS,
    MAX_GAP,
    NO_CLEARING,
    POLICY,
    FundingEvidence,
    FundingTimeline,
    FundingWindow,
    ProofFunding,
    finite_rate,
    published_rates,
    read_supplement,
    supplement_path,
)


class _RecheckFn(Protocol):
    def __call__(
        self,
        root: Path | str,
        secid: str,
        first: str,
        last: str,
        *,
        apply: bool = ...,
        fetch: Callable[..., dict[str, Any]] = ...,
    ) -> dict[str, Any]: ...


# Rebind the existing cross-module hook to its original callable: the owning
# module keeps the untyped definition, while this facade keeps its public name
# and runtime identity under whole-package strict typing.
recheck: _RecheckFn = cast(_RecheckFn, _funding_recheck.__dict__["recheck"])

__all__ = [
    "CARRY_SCHEMA",
    "DSVD_NOTICE",
    "FUNDING_REGIMES",
    "FUNDING_SCREEN_PAIRS",
    "FUNDING_SCREEN_SCHEMA",
    "FX_NEW_REGIME",
    "FX_PERPETUALS",
    "LAUNCH_PERPETUALS",
    "MAX_GAP",
    "NO_CLEARING",
    "POLICY",
    "STRATEGY_SCHEMA",
    "CarryError",
    "CarryStrategyError",
    "FundingEvidence",
    "FundingScreenError",
    "FundingTimeline",
    "FundingWindow",
    "ProofFunding",
    "build_carry_strategy",
    "build_carry_study",
    "fetch_history",
    "finite_rate",
    "published_rates",
    "read_supplement",
    "recheck",
    "screen_funding_pairs",
    "supplement_path",
]
