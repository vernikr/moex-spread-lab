"""Derived-cached ``/api/proof/catalog`` assembly (agent-performance-2, S4c).

The catalog is the most expensive read the daemon performs on real data and the
only one whose inputs are plain files, so it is cached under a fingerprint of
every contributing file (see ``core.derived_cache``). ``service.ProofService.catalog``
delegates here; the cache is byte-for-byte equivalent to a fresh computation.
"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol, cast

from moex_spread_lab.core.derived_cache import (
    derived_cache_enabled,
    fingerprint,
    load_bytes,
    store_bytes,
)
from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.timeframes import get_timeframe, timeframe_options
from moex_spread_lab.core.universe import (
    CALENDAR_UNIVERSE,
    MIN_DAILY_VOLUME,
    PAIR_UNIVERSE,
)
from moex_spread_lab.core.w0 import W0Error, canonical_bytes
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    catalog as _pair_catalog,
)


class CatalogService(Protocol):
    """The two attributes the catalog payload needs; keeps module import acyclic."""

    root: Path
    calendar_data: Any
    screener_inputs: Any


CATALOG_CACHE_NAMESPACE = "proof-catalog"


def _build_catalog(service: CatalogService, settings: ResearchSettings) -> dict[str, Any]:
    spec = get_timeframe(settings.timeframe)
    calendar: dict[str, Any]
    try:
        if not spec.supports_calendar:
            raise W0Error(
                f"{spec.label}: календарный proof не поддерживается на выбранном интервале."
            )
        calendar = service.calendar_data.catalog(
            initial_capital=settings.initial_capital_rub, timeframe=settings.timeframe
        )
    except (W0Error, OSError) as exc:
        calendar = {
            "till": None,
            "min_daily_volume": MIN_DAILY_VOLUME,
            "strategies": [],
            "fills": [],
            "assets": [],
            "error": str(exc),
        }
    pair = _pair_catalog(
        service.screener_inputs,
        initial_capital=settings.initial_capital_rub,
        timeframe=settings.timeframe,
    )
    # Expose only explicitly adapted plugins without changing the legacy
    # screener's fixed strategy controls. Availability is per timeframe; pair
    # data/volume/contract compatibility remains a proof-time decision.
    from moex_spread_lab.strategies.base import (
        carry_proof_plugin_ids,
        daily_pair_proof_plugin_ids,
    )
    from moex_spread_lab.strategies.registry import BUILTIN_STRATEGY_IDS

    from .strategy_catalog import get_strategy_registry

    # UI discovery surface: refresh sees a plugin installed at runtime.
    registry = get_strategy_registry(refresh=True)
    adapted = daily_pair_proof_plugin_ids(registry) | carry_proof_plugin_ids(registry)
    pair["proof_plugins"] = [
        {
            "id": p.strategy_id,
            "family_id": p.family_id,
            "label": p.label_ru,
            "adapter": getattr(p, "proof_adapter", None),
            "supported_fills": list(p.supported_fills),
            "requires_daily_bars": p.requires_daily_bars,
            "timeframe_supported": settings.timeframe == "1d" or not p.requires_daily_bars,
            "parameters": [spec.to_dict() for spec in p.parameter_schema()],
        }
        for p in registry
        # Built-in engines are already served by the native strategy controls; the
        # discovery surface lists drop-in plugins only.
        if p.strategy_id in adapted and p.strategy_id not in BUILTIN_STRATEGY_IDS
    ]
    return {
        **calendar,
        "initial_capital_rub": settings.initial_capital_rub,
        "timeframe": settings.timeframe,
        "timeframes": timeframe_options(),
        "universes": [CALENDAR_UNIVERSE, PAIR_UNIVERSE],
        "pair_screen": pair,
    }


def catalog_payload(service: CatalogService, settings: ResearchSettings) -> dict[str, Any]:
    parts = (settings.timeframe, str(settings.initial_capital_rub))
    key = None
    if derived_cache_enabled():
        key = fingerprint(service.root, CATALOG_CACHE_NAMESPACE, parts)
        cached = load_bytes(service.root, key)
        if cached is not None:
            try:
                hit = json.loads(cached)
            except ValueError:
                hit = None
            if isinstance(hit, dict):
                return cast(dict[str, Any], hit)
    payload = _build_catalog(service, settings)
    # Normalize through canonical JSON once, so the in-memory payload, the bytes
    # served over HTTP and the bytes stored in the cache are identical objects
    # (STRATEGIES/FILL_LABELS tuples become lists here and nowhere else).
    raw = canonical_bytes(payload)
    payload = json.loads(raw)
    if key is not None:
        # the cache is best-effort; the payload is authoritative
        with suppress(OSError):
            store_bytes(service.root, key, raw)
    return payload
