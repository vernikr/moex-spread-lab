"""MOEX Spread Lab Layer 1 data package: ISS intake, Parquet lake, retention, universe, and benchmark."""

from __future__ import annotations

from .benchmark import build_lineage, collect_benchmark_sources
from .iss import fetch_candle_series, fetch_iss_catalog
from .lake.data_lake import MarketDataStore, ParquetBarStore
from .retention.data_inventory import build_data_inventory
from .universe import ASSETS, PAIRS

__all__ = [
    "ASSETS",
    "PAIRS",
    "MarketDataStore",
    "ParquetBarStore",
    "build_data_inventory",
    "build_lineage",
    "collect_benchmark_sources",
    "fetch_candle_series",
    "fetch_iss_catalog",
]
