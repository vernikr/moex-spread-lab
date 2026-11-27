"""Re-export stub; canonical cache telemetry lives in ``core.derived_cache`` (#207)."""

from .derived_cache import collect_cache_metrics, record_cache_lookup

__all__ = ["collect_cache_metrics", "record_cache_lookup"]
