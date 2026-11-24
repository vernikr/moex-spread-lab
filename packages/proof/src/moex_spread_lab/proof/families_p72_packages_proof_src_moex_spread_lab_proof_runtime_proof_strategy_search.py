"""Family allowlist and shared type surface for the strategy search (Issue #93).

Split out of ``proof_strategy_search`` so that this file stays under the
code-health size ratchet and the compatibility allowlist can be read on its own.
Declarations and pure decisions only: no I/O, no search side effects.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from moex_spread_lab.core.timeframes import TimeframeSpec
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.strategies.base import StrategyRegistry

from .axis import Axis
from .compat_hooks_p72_138 import choose_hook as _choose_hook
from .strategy_catalog import get_proof_adapter_ids

_original_adapter_ids = get_proof_adapter_ids
_original_calendar_universe = CALENDAR_UNIVERSE
_family_hook_provider: Callable[[], tuple[Callable[..., frozenset[str]], str]] | None = None


def set_family_hook_provider(
    provider: Callable[[], tuple[Callable[..., frozenset[str]], str]],
) -> None:
    """Supply the owning facade hooks without a reverse import cycle."""
    global _family_hook_provider
    _family_hook_provider = provider


Obj = dict[str, Any]
Target = dict[str, Any]
Progress = Callable[[dict[str, Any]], None]
Attempts = list[dict[str, Any]]
Prepared = tuple[Obj, Any, Obj]
LabelMap = dict[str, str]
Axes = dict[str, Axis]


class PluginLike(Protocol):
    """The plugin surface the family allowlist reads."""

    strategy_id: str
    family_id: str
    short_label_ru: str
    supported_universes: frozenset[str]
    supported_fills: tuple[str, ...]
    requires_daily_bars: bool


class LandLike(Protocol):
    """Landscape surface the search measures through."""

    def measure(self, values: Obj | None) -> Obj: ...


class CohortLike(Protocol):
    """Structural view of the local and remote cohorts the search consumes.

    Members are read-only properties: the search never assigns them, and that
    keeps the protocol covariant over the concrete cohort's own attribute types.
    """

    @property
    def active(self) -> str: ...

    @property
    def axes(self) -> Axes: ...

    @property
    def seed(self) -> Obj: ...

    @property
    def members(self) -> list[tuple[Target, Any]]: ...

    @property
    def land(self) -> LandLike: ...

    def close(self) -> None: ...


class ServiceLike(Protocol):
    """The service facade; the calendar may be absent before the first replay."""

    root: Path


class SettingsLike(Protocol):
    """Research settings surface read by the search payload."""

    @property
    def timeframe(self) -> str: ...

    @property
    def random_seed(self) -> int: ...

    @property
    def price_fill(self) -> str: ...

    def metadata(self) -> dict[str, Any]: ...


Winner = tuple[Obj, CohortLike, "Obj | None", int, Attempts]


@dataclass
class Run:
    """Mutable per-request search state shared by the family loop."""

    request: Obj
    settings: SettingsLike
    check_cancelled: Callable[[], None]
    registry: StrategyRegistry
    labels: LabelMap
    active: str
    base_seed: int
    on_progress: Progress | None
    base: Obj = field(default_factory=dict)
    top: float = -float("inf")
    winner: Winner | None = None
    records: list[Obj] = field(default_factory=list)


def plugins(registry: StrategyRegistry) -> Iterator[PluginLike]:
    """Registry plugins behind the structural view the allowlist reads."""
    return iter(registry.values())


def _preferred_universe(
    active: Any, kinds: set[str], registry: StrategyRegistry, calendar: str | None = None
) -> str:
    """A mixed cohort follows the active editor when it can span both universes."""
    active_plugin = registry.get(active) if isinstance(active, str) and active in registry else None
    selected = CALENDAR_UNIVERSE if calendar is None else calendar
    if kinds == {selected}:
        return selected
    if (
        len(kinds) > 1
        and active_plugin is not None
        and selected in active_plugin.supported_universes
    ):
        return selected
    return next((u for u in sorted(kinds) if u != selected), selected)


def _group_families(
    registry: StrategyRegistry, adapter_ids: frozenset[str], timeframe: TimeframeSpec
) -> dict[str, list[PluginLike]]:
    """Compatible adapters by family, in registry order, funding carry last on intraday."""
    grouped: dict[str, list[PluginLike]] = {}
    for plugin in plugins(registry):
        if plugin.strategy_id not in adapter_ids:
            continue
        if plugin.requires_daily_bars and not timeframe.supports_funding_strategy:
            continue
        grouped.setdefault(plugin.family_id, []).append(plugin)
    return grouped


def families(
    request: Mapping[str, Any],
    targets: list[Target],
    timeframe: TimeframeSpec,
    registry: StrategyRegistry,
) -> list[str]:
    """One compatible adapter per registered family, in registry order.

    The allowlist is intentional: a plugin alone may lack the accounting adapter
    needed to replay it safely; a mixed cohort prefers the active editor's universe.
    """
    # Resolve the historical search facade globals at call time.
    facade_ids, facade_calendar = (
        _family_hook_provider()
        if _family_hook_provider is not None
        else (get_proof_adapter_ids, CALENDAR_UNIVERSE)
    )
    adapter_ids = _choose_hook(facade_ids, get_proof_adapter_ids, _original_adapter_ids)(registry)
    calendar = _choose_hook(facade_calendar, CALENDAR_UNIVERSE, _original_calendar_universe)
    kinds = {str(target["universe"]) for target in targets}
    preferred = _preferred_universe(request.get("strategy"), kinds, registry, calendar)
    result: list[str] = []
    for group in _group_families(registry, adapter_ids, timeframe).values():
        covered: set[str] = set[str]().union(*(p.supported_universes for p in group))
        if not kinds <= covered:
            continue
        match = next((p for p in group if preferred in p.supported_universes), None)
        if match is not None:
            result.append(match.strategy_id)
    return result
