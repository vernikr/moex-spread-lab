"""Parquet schemas and manifest helpers for `ParquetBarStore`.

The manifest is authoritative; obsolete DuckDB catalogs are left untouched and ignored.
Verification still compares file hashes, rows, and provenance against the source.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from typing import cast as _cast  # Narrow partially unknown pyarrow-stub callables.

import pyarrow as pa
import pyarrow.parquet as pq

from moex_spread_lab.core.w0 import sha256_file

from .bar_types import DataLakeError
from .io import atomic_write_json as _atomic_write_json
from .legacy_series import LegacyCandleSeries

DATA_LAKE_SCHEMA = "market-data-lake.v1"
BAR_DATASET_SCHEMA = "market-data-bars.v1"

BAR_SCHEMA = pa.schema(
    (
        pa.field("begin", pa.string(), nullable=False),
        pa.field("end", pa.string(), nullable=False),
        pa.field("open", pa.float64()),
        pa.field("high", pa.float64()),
        pa.field("low", pa.float64()),
        pa.field("close", pa.float64(), nullable=False),
        pa.field("value", pa.float64()),
        pa.field("volume", pa.int64()),
        pa.field("source_page_id", pa.int32()),
    )
)
SOURCE_SCHEMA = pa.schema(
    (
        pa.field("source_page_id", pa.int32(), nullable=False),
        pa.field("url", pa.string()),
        pa.field("received_at", pa.string()),
        pa.field("raw_response_sha256", pa.string()),
        pa.field("first_begin", pa.string(), nullable=False),
        pa.field("last_begin", pa.string(), nullable=False),
        pa.field("row_count", pa.int32(), nullable=False),
    )
)


def atomic_write_parquet(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
        _cast(Any, pq.write_table)(table, temporary, compression="zstd", version="2.6")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parse_lake_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        value: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataLakeError(f"cannot read data-lake manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise DataLakeError("unsupported data-lake manifest schema")
    value = _cast(dict[str, Any], value)
    if value.get("schema_version") != DATA_LAKE_SCHEMA:
        raise DataLakeError("unsupported data-lake manifest schema")
    if not isinstance(value.get("datasets"), list):
        raise DataLakeError("data-lake manifest datasets must be a list")
    return value


def upsert_manifest(
    manifest_path: Path, current_manifest: dict[str, Any], record: dict[str, Any]
) -> None:
    manifest = dict(current_manifest)
    datasets = [
        _cast("dict[str, Any]", entry) for entry in manifest["datasets"] if isinstance(entry, dict)
    ]
    previous = next(
        (entry for entry in datasets if entry.get("dataset_id") == record["dataset_id"]), None
    )
    if previous is not None and {k: v for k, v in previous.items() if k != "created_at"} != {
        k: v for k, v in record.items() if k != "created_at"
    }:
        revisions = manifest.setdefault("dataset_revisions", [])
        if not isinstance(revisions, list):
            raise DataLakeError("data-lake manifest dataset_revisions must be a list")
        _cast("list[Any]", revisions).append(
            {
                "dataset_id": record["dataset_id"],
                "replaced_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "record": previous,
            }
        )
    entries = [entry for entry in datasets if entry.get("dataset_id") != record["dataset_id"]]
    entries.append(record)
    manifest["datasets"] = sorted(entries, key=lambda item: item["dataset_id"])
    manifest["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    _atomic_write_json(manifest_path, manifest)


def _relative_to_root(root: Path, path: Path) -> str:
    return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)


def build_import_record(
    root: Path, series: LegacyCandleSeries, bars_path: Path, sources_path: Path
) -> dict[str, Any]:
    return {
        "dataset_id": series.dataset_id,
        "schema_version": BAR_DATASET_SCHEMA,
        "secid": series.secid,
        "board": series.board,
        "interval": series.interval,
        "timeframe": series.timeframe,
        "row_count": len(series.bars),
        "first_begin": series.bars[0]["begin"],
        "last_begin": series.bars[-1]["begin"],
        "from": series.from_date,
        "till": series.till_date,
        "legacy_path": _relative_to_root(root, series.path),
        "legacy_sha256": series.sha256,
        "legacy_metadata": series.legacy_metadata,
        "row_semantics": series.row_semantics,
        "bars_path": _relative_to_root(root, bars_path),
        "sources_path": _relative_to_root(root, sources_path),
        "parquet_sha256": sha256_file(bars_path),
        "sources_sha256": sha256_file(sources_path),
        "source_page_count": len(series.sources),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def verify_dataset_against_legacy(
    root: Path, series: LegacyCandleSeries, record: dict[str, Any]
) -> dict[str, Any]:
    if record.get("row_count") != len(series.bars):
        raise DataLakeError(f"manifest row count differs from legacy rows: {series.dataset_id}")
    if record.get("source_page_count") != len(series.sources):
        raise DataLakeError(
            f"manifest source count differs from legacy provenance: {series.dataset_id}"
        )
    if record.get("legacy_sha256") != series.sha256:
        raise DataLakeError(f"legacy SHA-256 differs from imported dataset: {series.dataset_id}")
    if record.get("legacy_metadata") != series.legacy_metadata:
        raise DataLakeError(f"legacy metadata differs from imported dataset: {series.dataset_id}")
    if record.get("row_semantics") != series.row_semantics:
        raise DataLakeError(
            f"legacy row semantics differ from imported dataset: {series.dataset_id}"
        )
    bars_path = root / record["bars_path"]
    sources_path = root / record["sources_path"]
    if sha256_file(bars_path) != record.get("parquet_sha256"):
        raise DataLakeError(f"bars Parquet SHA-256 differs from manifest: {series.dataset_id}")
    if sha256_file(sources_path) != record.get("sources_sha256"):
        raise DataLakeError(f"sources Parquet SHA-256 differs from manifest: {series.dataset_id}")
    if _cast(Any, pq.ParquetFile(bars_path).read)().to_pylist() != series.bars:
        raise DataLakeError(f"Parquet bars differ from legacy rows: {series.dataset_id}")
    if _cast(Any, pq.ParquetFile(sources_path).read)().to_pylist() != series.sources:
        raise DataLakeError(
            f"Parquet source manifest differs from legacy provenance: {series.dataset_id}"
        )
    return {
        "schema_version": BAR_DATASET_SCHEMA,
        "status": "verified",
        "dataset_id": series.dataset_id,
        "row_count": len(series.bars),
        "source_page_count": len(series.sources),
        "legacy_sha256": series.sha256,
        "parquet_sha256": record["parquet_sha256"],
    }


__all__ = [
    "BAR_DATASET_SCHEMA",
    "BAR_SCHEMA",
    "DATA_LAKE_SCHEMA",
    "SOURCE_SCHEMA",
    "build_import_record",
    "parse_lake_manifest",
    "upsert_manifest",
    "verify_dataset_against_legacy",
]
