"""Local Parquet/Zstandard bar store with manifest and legacy verification."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import pyarrow as pa
import pyarrow.parquet as pq

from moex_spread_lab.core.w0 import sha256_file

from .bar_types import (
    LEGACY_CANDLE_SCHEMA,
    BarSeries,
    DataLakeError,
    reject_sealed_table_write,
    verify_archived_record,
)
from .legacy_series import LegacyCandleSeries
from .legacy_series import component as _component
from .legacy_series import read_legacy_series as _read_legacy_series
from .parquet_catalog import (
    BAR_DATASET_SCHEMA,
    BAR_SCHEMA,
    DATA_LAKE_SCHEMA,
    SOURCE_SCHEMA,
    build_import_record,
    parse_lake_manifest,
    upsert_manifest,
    verify_dataset_against_legacy,
)
from .parquet_catalog import atomic_write_parquet as _atomic_write_parquet
from .raw_archive import RawResponseArchive


class TableCalls(Protocol):
    """`pyarrow.Table` members consumed by the store, typed over the incomplete stubs."""

    @classmethod
    def from_pylist(
        cls, mapping: Sequence[Mapping[str, Any]], schema: pa.Schema | None = ...
    ) -> pa.Table: ...

    def to_pylist(self) -> list[dict[str, Any]]: ...


_TABLE = cast("type[TableCalls]", pa.Table)


def table_from_rows(rows: Sequence[Mapping[str, Any]], schema: pa.Schema) -> pa.Table:
    """Build a table from object-dict rows under an explicit schema."""
    return cast(TableCalls, _TABLE).from_pylist(rows, schema=schema)


def table_rows(table: pa.Table) -> list[dict[str, Any]]:
    """Convert a table to the object-dict rows the store consumes."""
    return cast(TableCalls, table).to_pylist()


def read_table(path: Path, columns: Iterable[str] | None, filters: Any = None) -> pa.Table:
    """Read one Parquet file, optionally projecting columns or pushing row filters."""
    projected = list(columns) if columns is not None else None
    if filters is None:
        return cast(pa.Table, cast(Any, pq.ParquetFile(path)).read(columns=projected))
    # Stub 19.4 rejects valid string comparison filters.
    return cast(pa.Table, cast(Any, pq.read_table)(path, columns=projected, filters=filters))


def as_dict(value: object) -> dict[str, Any] | None:
    """Return a JSON object mapping, or None for any other payload shape."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def manifest_records(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Narrow the manifest `datasets` list to object records, ignoring other items."""
    entries: object = manifest["datasets"]
    if not isinstance(entries, list):
        raise DataLakeError("data-lake manifest datasets must be a list")
    return [
        record for item in cast("list[object]", entries) if (record := as_dict(item)) is not None
    ]


class ParquetBarStore:
    """Manifest-backed local Parquet/Zstandard store."""

    def __init__(self, root: Path, lake_root: Path | None = None) -> None:
        self.root = root.resolve()
        candidate = lake_root if lake_root is not None else self.root / "data-lake"
        self.lake_root = candidate.resolve()
        self.bars_root = self.lake_root / "bars"
        self.sources_root = self.lake_root / "sources"
        self.catalog_path = self.lake_root / "catalog.duckdb"
        self.manifest_path = self.lake_root / "manifest.json"
        self._manifest_sig: tuple[int, int] | None = None
        self._manifest_cache: dict[str, Any] | None = None
        self._manifest_by_id: dict[str, dict[str, Any]] = {}
        self._manifest_by_legacy: dict[Path, dict[str, Any]] = {}

    def _paths(self, series: LegacyCandleSeries) -> tuple[Path, Path]:
        prefix = Path(f"timeframe={series.timeframe}") / f"secid={series.secid}"
        return (
            self.bars_root / prefix / "data.parquet",
            self.sources_root / prefix / "sources.parquet",
        )

    def _index_manifest(self, value: dict[str, Any]) -> None:
        by_id: dict[str, dict[str, Any]] = {}
        by_legacy: dict[Path, dict[str, Any]] = {}
        for record in manifest_records(value):
            ds_id = record.get("dataset_id")
            if isinstance(ds_id, str):
                by_id[ds_id] = record
            lp = record.get("legacy_path")
            if isinstance(lp, str):
                with contextlib.suppress(OSError):
                    by_legacy[self._record_path(lp).resolve()] = record
        self._manifest_by_id = by_id
        self._manifest_by_legacy = by_legacy

    def _read_manifest(self) -> dict[str, Any]:
        try:
            st = self.manifest_path.stat()
        except OSError:
            self._manifest_sig = None
            self._manifest_cache = None
            self._manifest_by_id.clear()
            self._manifest_by_legacy.clear()
            return {"schema_version": DATA_LAKE_SCHEMA, "datasets": []}
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._manifest_sig and self._manifest_cache is not None:
            return self._manifest_cache
        value = parse_lake_manifest(self.manifest_path)
        self._index_manifest(value)
        self._manifest_sig = sig
        self._manifest_cache = value
        return value

    def read_manifest(self) -> dict[str, Any]:
        """Public manifest accessor."""
        return self._read_manifest()

    def _upsert_manifest(self, record: dict[str, Any]) -> None:
        upsert_manifest(self.manifest_path, self._read_manifest(), record)

    def import_legacy(self, legacy_path: Path, *, replace: bool = False) -> dict[str, Any]:
        series = _read_legacy_series(legacy_path)
        bars_path, sources_path = self._paths(series)
        reject_sealed_table_write(self.root, bars_path)
        if not replace and (bars_path.exists() or sources_path.exists()):
            raise DataLakeError(
                f"data-lake dataset already exists: {series.dataset_id}; use --replace"
            )
        _atomic_write_parquet(bars_path, table_from_rows(series.bars, BAR_SCHEMA))
        _atomic_write_parquet(sources_path, table_from_rows(series.sources, SOURCE_SCHEMA))
        record = build_import_record(self.root, series, bars_path, sources_path)
        self._upsert_manifest(record)
        return record

    def _record(self, secid: str, timeframe: str) -> dict[str, Any]:
        dataset_id = f"{_component(timeframe, 'timeframe')}/{_component(secid, 'SECID')}"
        self._read_manifest()
        record = self._manifest_by_id.get(dataset_id)
        if record is not None:
            return record
        raise DataLakeError(f"data-lake dataset is not registered: {dataset_id}")

    def _record_path(self, value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else self.root / candidate

    def record_path(self, value: str) -> Path:
        """Resolve a manifest path; keeps the private override hook."""
        return self._record_path(value)

    def record_for_legacy_path(self, legacy_path: Path) -> dict[str, Any] | None:
        """Return the registered record only for this exact legacy source path."""
        try:
            selected = legacy_path.resolve()
        except OSError:
            return None
        self._read_manifest()
        return self._manifest_by_legacy.get(selected)

    def read_series(
        self,
        secid: str,
        timeframe: str,
        *,
        columns: Iterable[str] | None = None,
        source_path: Path | None = None,
        source_sha256: str | None = None,
    ) -> BarSeries:
        """Read a registered dataset as rows consumed by research logic."""
        record = self._record(secid, timeframe)
        bars_path = self._record_path(record["bars_path"])
        semantics = as_dict(record.get("row_semantics"))
        price_proxy_kind = semantics.get("price_proxy_kind") if semantics is not None else None
        rows: list[dict[str, Any]] = []
        for row in table_rows(read_table(bars_path, columns)):
            if price_proxy_kind is not None:
                row["price_proxy_kind"] = price_proxy_kind
            rows.append(row)
        selected_path = source_path or self._record_path(record["legacy_path"])
        try:
            selected_stat = selected_path.stat()
            source_fingerprint: tuple[Any, ...] = (selected_stat.st_mtime_ns, selected_stat.st_size)
        except OSError:
            source_fingerprint = ()
        bars_stat = bars_path.stat()
        return BarSeries(
            schema_version=LEGACY_CANDLE_SCHEMA,
            secid=record.get("secid") if isinstance(record.get("secid"), str) else secid,
            interval=record.get("interval") if isinstance(record.get("interval"), int) else None,
            timeframe=record.get("timeframe")
            if isinstance(record.get("timeframe"), str)
            else timeframe,
            rows=tuple(rows),
            source_path=str(selected_path),
            source_sha256=source_sha256 or str(record["legacy_sha256"]),
            backend="parquet",
            cache_key=(
                "parquet",
                str(selected_path.resolve()),
                *source_fingerprint,
                str(bars_path.resolve()),
                bars_stat.st_mtime_ns,
                bars_stat.st_size,
            ),
            metadata=record,
        )

    def read_bars(
        self,
        secid: str,
        timeframe: str,
        *,
        columns: Iterable[str] | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> pa.Table:
        record = self._record(secid, timeframe)
        bars_path = self.root / record["bars_path"]
        table = read_table(bars_path, columns)
        if start is not None or end is not None:
            if "begin" not in table.column_names:
                raise DataLakeError("date filtering requires the begin column")
            filters: list[tuple[str, str, str]] = []
            if start is not None:
                filters.append(("begin", ">=", start))
            if end is not None:
                filters.append(("begin", "<=", end))
            return read_table(bars_path, columns, filters)
        return table

    def verify_legacy(self, legacy_path: Path) -> dict[str, Any]:
        if not legacy_path.exists():
            return verify_archived_record(self, legacy_path)
        series = _read_legacy_series(legacy_path)
        record = self._record(series.secid, series.timeframe)
        return verify_dataset_against_legacy(self.root, series, record)

    @property
    def raw_archive(self) -> RawResponseArchive:
        return RawResponseArchive(self.lake_root)

    def upsert_legacy(self, legacy_path: Path) -> dict[str, Any]:
        """Atomically replace a dataset from a merged series without duplicate keys."""
        series = _read_legacy_series(legacy_path)
        try:
            self._record(series.secid, series.timeframe)
            replace = True
        except DataLakeError:
            replace = False
        return self.import_legacy(series.path, replace=replace)

    def verify_all(self) -> dict[str, Any]:
        records = manifest_records(self._read_manifest())
        datasets = [
            self.verify_legacy(self._record_path(str(record.get("legacy_path", ""))))
            for record in sorted(records, key=lambda item: str(item.get("dataset_id")))
        ]
        return {
            "status": "verified",
            "dataset_count": len(datasets),
            "datasets": datasets,
            "raw": self.raw_archive.verify(),
        }

    def compact(self, *, force: bool = False) -> dict[str, Any]:
        """Keep each registered dataset as one Zstd bars file and one source file."""
        records = manifest_records(self._read_manifest())
        results: list[dict[str, Any]] = []
        for record in sorted(records, key=lambda item: str(item.get("dataset_id"))):
            dataset_id = str(record.get("dataset_id"))
            if not force:
                results.append({"dataset_id": dataset_id, "status": "already_compact"})
                continue
            bars_path = self._record_path(str(record["bars_path"]))
            reject_sealed_table_write(self.root, bars_path)
            sources_path = self._record_path(str(record["sources_path"]))
            bars = read_table(bars_path, None)
            sources = read_table(sources_path, None)
            _atomic_write_parquet(bars_path, bars)
            _atomic_write_parquet(sources_path, sources)
            updated = dict(record)
            updated["parquet_sha256"] = sha256_file(bars_path)
            updated["sources_sha256"] = sha256_file(sources_path)
            updated["compacted_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            self._upsert_manifest(updated)
            self.verify_legacy(self._record_path(str(updated["legacy_path"])))
            results.append(
                {"dataset_id": dataset_id, "status": "compacted", "row_count": bars.num_rows}
            )
        return {
            "status": "compacted"
            if any(item["status"] == "compacted" for item in results)
            else "already_compact",
            "dataset_count": len(results),
            "datasets": results,
        }


__all__ = [
    "BAR_DATASET_SCHEMA",
    "BAR_SCHEMA",
    "DATA_LAKE_SCHEMA",
    "SOURCE_SCHEMA",
    "ParquetBarStore",
]
