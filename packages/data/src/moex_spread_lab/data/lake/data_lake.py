"""Typed local Parquet store and importer for legacy ISS candle series.

Public and internal facade re-exporting the decomposed `data.lake` submodules
(`bar_types`, `legacy_series`, `raw_archive`, `parquet_catalog`, `parquet_store`,
`market_store`) so all callers and import shims remain backwards-compatible.
"""

from __future__ import annotations

from moex_spread_lab.core.w0 import sha256_file

from .bar_types import (
    LEGACY_CANDLE_SCHEMA,
    BarSeries,
    BarStore,
    DataLakeError,
    SealedH1Reader,
    reject_sealed_table_write,
    verify_archived_record,
)
from .io import atomic_write_json as _atomic_write_json
from .legacy_series import LegacyCandleSeries, LegacyJsonBarStore
from .legacy_series import read_legacy_series as _read_legacy_series
from .market_store import MarketDataStore
from .parquet_catalog import (
    BAR_DATASET_SCHEMA,
    BAR_SCHEMA,
    DATA_LAKE_SCHEMA,
    SOURCE_SCHEMA,
)
from .parquet_store import ParquetBarStore
from .raw_archive import RAW_ARCHIVE_SCHEMA, RawResponseArchive

__all__ = [
    "BAR_DATASET_SCHEMA",
    "BAR_SCHEMA",
    "DATA_LAKE_SCHEMA",
    "LEGACY_CANDLE_SCHEMA",
    "RAW_ARCHIVE_SCHEMA",
    "SOURCE_SCHEMA",
    "BarSeries",
    "BarStore",
    "DataLakeError",
    "LegacyCandleSeries",
    "LegacyJsonBarStore",
    "MarketDataStore",
    "ParquetBarStore",
    "RawResponseArchive",
    "SealedH1Reader",
    "_atomic_write_json",
    "_read_legacy_series",
    "reject_sealed_table_write",
    "sha256_file",
    "verify_archived_record",
]
