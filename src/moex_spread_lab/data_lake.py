"""Typed local Parquet store and importer for legacy ISS candle series."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .w0 import W0Error, canonical_bytes, sha256_file


DATA_LAKE_SCHEMA = "market-data-lake.v1"
BAR_DATASET_SCHEMA = "market-data-bars.v1"
LEGACY_CANDLE_SCHEMA = "iss-candles.v1"
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")

BAR_SCHEMA = pa.schema((
    pa.field("begin", pa.string(), nullable=False),
    pa.field("end", pa.string(), nullable=False),
    pa.field("open", pa.float64()),
    pa.field("high", pa.float64()),
    pa.field("low", pa.float64()),
    pa.field("close", pa.float64(), nullable=False),
    pa.field("value", pa.float64()),
    pa.field("volume", pa.int64()),
    pa.field("source_page_id", pa.int32()),
))
SOURCE_SCHEMA = pa.schema((
    pa.field("source_page_id", pa.int32(), nullable=False),
    pa.field("url", pa.string()),
    pa.field("received_at", pa.string()),
    pa.field("raw_response_sha256", pa.string()),
    pa.field("first_begin", pa.string(), nullable=False),
    pa.field("last_begin", pa.string(), nullable=False),
    pa.field("row_count", pa.int32(), nullable=False),
))


class DataLakeError(W0Error):
    """Raised when an on-disk candle dataset cannot pass the data-lake contract."""


@dataclass(frozen=True)
class LegacyCandleSeries:
    path: Path
    sha256: str
    secid: str
    board: str | None
    interval: int
    timeframe: str
    from_date: str | None
    till_date: str | None
    legacy_metadata: dict[str, Any]
    row_semantics: dict[str, Any]
    bars: list[dict[str, Any]]
    sources: list[dict[str, Any]]

    @property
    def dataset_id(self) -> str:
        return f"{self.timeframe}/{self.secid}"


def _component(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_COMPONENT.fullmatch(value):
        raise DataLakeError(f"invalid {label} for data-lake path: {value!r}")
    return value


def _number(value: Any, label: str, *, required: bool = False) -> float | None:
    if value is None:
        if required:
            raise DataLakeError(f"legacy candle {label} is missing")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataLakeError(f"legacy candle {label} must be numeric or null")
    return float(value)


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DataLakeError(f"legacy candle {label} must be a non-empty string")
    return value


def _integer(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataLakeError(f"legacy candle {label} must be an integer or null")
    integer = int(value)
    if integer != value:
        raise DataLakeError(f"legacy candle {label} must be an integer or null")
    return integer


def _source_key(source: dict[str, Any]) -> bytes:
    return canonical_bytes({
        "url": source.get("url"),
        "received_at": source.get("received_at"),
        "raw_response_sha256": source.get("raw_response_sha256"),
    })


def _read_legacy_series(path: Path) -> LegacyCandleSeries:
    resolved = path.resolve()
    try:
        raw = resolved.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataLakeError(f"cannot read legacy candle JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != LEGACY_CANDLE_SCHEMA:
        raise DataLakeError(f"legacy file is not {LEGACY_CANDLE_SCHEMA}: {path}")
    secid = _component(payload.get("secid"), "SECID")
    timeframe = _component(payload.get("timeframe"), "timeframe")
    interval = payload.get("interval")
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise DataLakeError(f"legacy candle interval is invalid: {path}")
    board = payload.get("board")
    if board is not None and not isinstance(board, str):
        raise DataLakeError(f"legacy candle board is invalid: {path}")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise DataLakeError(f"legacy candle series has no rows: {path}")
    legacy_metadata = {key: value for key, value in payload.items() if key != "rows"}
    _unset = object()
    semantic_values: dict[str, Any] = {
        "price_proxy_kind": _unset,
        "price_proxy_assumption": _unset,
    }

    bars: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    source_ids: dict[bytes, int] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise DataLakeError(f"legacy candle row is not an object: {path}[{index}]")
        begin = _timestamp(row.get("begin"), "begin")
        end = _timestamp(row.get("end"), "end")
        close = _number(row.get("close"), "close", required=True)
        for key in ("price_proxy_kind", "price_proxy_assumption"):
            value = row.get(key)
            if value is not None and not isinstance(value, str):
                raise DataLakeError(f"legacy candle {key} must be a string or null")
            previous = semantic_values[key]
            if previous is _unset:
                semantic_values[key] = value
            elif value != previous:
                raise DataLakeError(f"legacy candle {key} is not constant: {path}")
        price_proxy = row.get("price_proxy")
        if price_proxy is not None:
            if isinstance(price_proxy, bool) or not isinstance(price_proxy, (int, float, str)):
                raise DataLakeError(f"legacy candle price_proxy is invalid: {path}[{index}]")
            try:
                proxy_value = float(price_proxy)
            except ValueError as exc:
                raise DataLakeError(f"legacy candle price_proxy is not numeric: {path}[{index}]") from exc
            if proxy_value != close:
                raise DataLakeError(f"legacy candle price_proxy differs from close: {path}[{index}]")
        source_page_id: int | None = None
        source = row.get("source")
        if source is not None:
            if not isinstance(source, dict):
                raise DataLakeError(f"legacy candle source is invalid: {path}[{index}]")
            page_provenance: dict[str, str | None] = {}
            for field in ("url", "received_at", "raw_response_sha256"):
                value = source.get(field)
                if value is not None and not isinstance(value, str):
                    raise DataLakeError(f"legacy candle source {field} must be a string or null: {path}[{index}]")
                page_provenance[field] = value
            raw_hash = page_provenance["raw_response_sha256"]
            if raw_hash is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", raw_hash):
                raise DataLakeError(f"legacy candle source raw_response_sha256 is invalid: {path}[{index}]")
            key = _source_key(page_provenance)
            source_page_id = source_ids.get(key)
            if source_page_id is None:
                source_page_id = len(sources)
                source_ids[key] = source_page_id
                sources.append({
                    "source_page_id": source_page_id,
                    **page_provenance,
                    "first_begin": begin,
                    "last_begin": begin,
                    "row_count": 0,
                })
            page = sources[source_page_id]
            page["last_begin"] = begin
            page["row_count"] += 1
        bars.append({
            "begin": begin,
            "end": end,
            "open": _number(row.get("open"), "open"),
            "high": _number(row.get("high"), "high"),
            "low": _number(row.get("low"), "low"),
            "close": close,
            "value": _number(row.get("value"), "value"),
            "volume": _integer(row.get("volume"), "volume"),
            "source_page_id": source_page_id,
        })
    if any(left["begin"] >= right["begin"] for left, right in zip(bars, bars[1:])):
        raise DataLakeError(f"legacy candle timestamps are not strictly increasing: {path}")
    row_semantics = {
        "price_proxy_kind": semantic_values["price_proxy_kind"],
        "price_proxy_assumption": semantic_values["price_proxy_assumption"],
        "price_proxy_matches_close": True,
    }
    return LegacyCandleSeries(
        path=resolved,
        sha256=sha256_file(resolved),
        secid=secid,
        board=board,
        interval=interval,
        timeframe=timeframe,
        from_date=payload.get("from") if isinstance(payload.get("from"), str) else None,
        till_date=payload.get("till") if isinstance(payload.get("till"), str) else None,
        legacy_metadata=legacy_metadata,
        row_semantics=row_semantics,
        bars=bars,
        sources=sources,
    )


def _atomic_write_parquet(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
        pq.write_table(table, temporary, compression="zstd", version="2.6")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ParquetBarStore:
    """Local Parquet/Zstandard store; no runtime reader is switched to it in Phase 2."""

    def __init__(self, root: Path, lake_root: Path | None = None):
        self.root = root.resolve()
        candidate = lake_root if lake_root is not None else self.root / "data-lake"
        self.lake_root = candidate.resolve()
        self.bars_root = self.lake_root / "bars"
        self.sources_root = self.lake_root / "sources"
        self.catalog_path = self.lake_root / "catalog.duckdb"
        self.manifest_path = self.lake_root / "manifest.json"

    def _paths(self, series: LegacyCandleSeries) -> tuple[Path, Path]:
        prefix = Path(f"timeframe={series.timeframe}") / f"secid={series.secid}"
        return self.bars_root / prefix / "data.parquet", self.sources_root / prefix / "sources.parquet"

    def _read_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            return {"schema_version": DATA_LAKE_SCHEMA, "datasets": []}
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataLakeError(f"cannot read data-lake manifest: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != DATA_LAKE_SCHEMA:
            raise DataLakeError("unsupported data-lake manifest schema")
        if not isinstance(value.get("datasets"), list):
            raise DataLakeError("data-lake manifest datasets must be a list")
        return value

    def _upsert_catalog(self, record: dict[str, Any], sources: list[dict[str, Any]]) -> None:
        self.lake_root.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(self.catalog_path)) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS bar_datasets (
                    dataset_id VARCHAR PRIMARY KEY,
                    secid VARCHAR NOT NULL,
                    board VARCHAR,
                    interval INTEGER NOT NULL,
                    timeframe VARCHAR NOT NULL,
                    bars_path VARCHAR NOT NULL,
                    sources_path VARCHAR NOT NULL,
                    row_count BIGINT NOT NULL,
                    first_begin VARCHAR NOT NULL,
                    last_begin VARCHAR NOT NULL,
                    legacy_path VARCHAR NOT NULL,
                    legacy_sha256 VARCHAR NOT NULL,
                    parquet_sha256 VARCHAR NOT NULL,
                    sources_sha256 VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS source_pages (
                    dataset_id VARCHAR NOT NULL,
                    source_page_id INTEGER NOT NULL,
                    url VARCHAR,
                    received_at VARCHAR,
                    raw_response_sha256 VARCHAR,
                    first_begin VARCHAR NOT NULL,
                    last_begin VARCHAR NOT NULL,
                    row_count BIGINT NOT NULL,
                    PRIMARY KEY(dataset_id, source_page_id)
                )
            """)
            connection.execute("DELETE FROM source_pages WHERE dataset_id = ?", [record["dataset_id"]])
            connection.execute("DELETE FROM bar_datasets WHERE dataset_id = ?", [record["dataset_id"]])
            connection.execute("""
                INSERT INTO bar_datasets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                record[key] for key in (
                    "dataset_id", "secid", "board", "interval", "timeframe", "bars_path", "sources_path",
                    "row_count", "first_begin", "last_begin", "legacy_path", "legacy_sha256",
                    "parquet_sha256", "sources_sha256", "created_at",
                )
            ])
            if sources:
                connection.executemany("""
                    INSERT INTO source_pages VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    [record["dataset_id"], *[
                        source[key]
                        for key in (
                            "source_page_id", "url", "received_at", "raw_response_sha256",
                            "first_begin", "last_begin", "row_count",
                        )
                    ]]
                    for source in sources
                ])

    def _upsert_manifest(self, record: dict[str, Any]) -> None:
        manifest = self._read_manifest()
        entries = [entry for entry in manifest["datasets"] if entry.get("dataset_id") != record["dataset_id"]]
        entries.append(record)
        manifest["datasets"] = sorted(entries, key=lambda item: item["dataset_id"])
        manifest["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _atomic_write_json(self.manifest_path, manifest)

    def import_legacy(self, legacy_path: Path, *, replace: bool = False) -> dict[str, Any]:
        series = _read_legacy_series(legacy_path)
        bars_path, sources_path = self._paths(series)
        if not replace and (bars_path.exists() or sources_path.exists()):
            raise DataLakeError(f"data-lake dataset already exists: {series.dataset_id}; use --replace")
        bars = pa.Table.from_pylist(series.bars, schema=BAR_SCHEMA)
        sources = pa.Table.from_pylist(series.sources, schema=SOURCE_SCHEMA)
        _atomic_write_parquet(bars_path, bars)
        _atomic_write_parquet(sources_path, sources)
        record = {
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
            "legacy_path": str(series.path.relative_to(self.root)) if series.path.is_relative_to(self.root) else str(series.path),
            "legacy_sha256": series.sha256,
            "legacy_metadata": series.legacy_metadata,
            "row_semantics": series.row_semantics,
            "bars_path": str(bars_path.relative_to(self.root)) if bars_path.is_relative_to(self.root) else str(bars_path),
            "sources_path": str(sources_path.relative_to(self.root)) if sources_path.is_relative_to(self.root) else str(sources_path),
            "parquet_sha256": sha256_file(bars_path),
            "sources_sha256": sha256_file(sources_path),
            "source_page_count": len(series.sources),
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self._upsert_catalog(record, series.sources)
        self._upsert_manifest(record)
        return record

    def _record(self, secid: str, timeframe: str) -> dict[str, Any]:
        dataset_id = f"{_component(timeframe, 'timeframe')}/{_component(secid, 'SECID')}"
        manifest = self._read_manifest()
        for record in manifest["datasets"]:
            if isinstance(record, dict) and record.get("dataset_id") == dataset_id:
                return record
        raise DataLakeError(f"data-lake dataset is not registered: {dataset_id}")

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
        table = pq.ParquetFile(bars_path).read(columns=list(columns) if columns is not None else None)
        if start is not None or end is not None:
            if "begin" not in table.column_names:
                raise DataLakeError("date filtering requires the begin column")
            mask = None
            if start is not None:
                mask = pc.greater_equal(table["begin"], pa.scalar(start))
            if end is not None:
                upper = pc.less_equal(table["begin"], pa.scalar(end))
                mask = upper if mask is None else pc.and_(mask, upper)
            table = table.filter(mask)
        return table

    def verify_legacy(self, legacy_path: Path) -> dict[str, Any]:
        series = _read_legacy_series(legacy_path)
        record = self._record(series.secid, series.timeframe)
        if record.get("legacy_sha256") != series.sha256:
            raise DataLakeError(f"legacy SHA-256 differs from imported dataset: {series.dataset_id}")
        if record.get("legacy_metadata") != series.legacy_metadata:
            raise DataLakeError(f"legacy metadata differs from imported dataset: {series.dataset_id}")
        if record.get("row_semantics") != series.row_semantics:
            raise DataLakeError(f"legacy row semantics differ from imported dataset: {series.dataset_id}")
        bars_path = self.root / record["bars_path"]
        sources_path = self.root / record["sources_path"]
        if sha256_file(bars_path) != record.get("parquet_sha256"):
            raise DataLakeError(f"bars Parquet SHA-256 differs from manifest: {series.dataset_id}")
        if sha256_file(sources_path) != record.get("sources_sha256"):
            raise DataLakeError(f"sources Parquet SHA-256 differs from manifest: {series.dataset_id}")
        actual_bars = pq.ParquetFile(bars_path).read().to_pylist()
        actual_sources = pq.ParquetFile(sources_path).read().to_pylist()
        if actual_bars != series.bars:
            raise DataLakeError(f"Parquet bars differ from legacy rows: {series.dataset_id}")
        if actual_sources != series.sources:
            raise DataLakeError(f"Parquet source manifest differs from legacy provenance: {series.dataset_id}")
        with duckdb.connect(str(self.catalog_path), read_only=True) as connection:
            catalog = connection.execute(
                "SELECT row_count, legacy_sha256, parquet_sha256, sources_sha256 FROM bar_datasets WHERE dataset_id = ?",
                [series.dataset_id],
            ).fetchone()
        if catalog != (len(series.bars), series.sha256, record["parquet_sha256"], record["sources_sha256"]):
            raise DataLakeError(f"DuckDB catalog differs from manifest: {series.dataset_id}")
        return {
            "schema_version": BAR_DATASET_SCHEMA,
            "status": "verified",
            "dataset_id": series.dataset_id,
            "row_count": len(series.bars),
            "source_page_count": len(series.sources),
            "legacy_sha256": series.sha256,
            "parquet_sha256": record["parquet_sha256"],
        }
