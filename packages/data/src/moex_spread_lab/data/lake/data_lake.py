"""Typed local Parquet store and importer for legacy ISS candle series."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from moex_spread_lab.core.w0 import canonical_bytes, sha256_file

from .bar_types import (
    LEGACY_CANDLE_SCHEMA,
    BarSeries,
    DataLakeError,
    SealedH1Reader,
    _atomic_write_json,
    reject_sealed_table_write,
    verify_archived_record,
)

DATA_LAKE_SCHEMA = "market-data-lake.v1"
BAR_DATASET_SCHEMA = "market-data-bars.v1"
RAW_ARCHIVE_SCHEMA = "market-data-raw.v1"
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")

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
    return canonical_bytes(
        {
            "url": source.get("url"),
            "received_at": source.get("received_at"),
            "raw_response_sha256": source.get("raw_response_sha256"),
        }
    )


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
                raise DataLakeError(
                    f"legacy candle price_proxy is not numeric: {path}[{index}]"
                ) from exc
            if proxy_value != close:
                raise DataLakeError(
                    f"legacy candle price_proxy differs from close: {path}[{index}]"
                )
        source_page_id: int | None = None
        source = row.get("source")
        if source is not None:
            if not isinstance(source, dict):
                raise DataLakeError(f"legacy candle source is invalid: {path}[{index}]")
            page_provenance: dict[str, str | None] = {}
            for field in ("url", "received_at", "raw_response_sha256"):
                value = source.get(field)
                if value is not None and not isinstance(value, str):
                    raise DataLakeError(
                        f"legacy candle source {field} must be a string or null: {path}[{index}]"
                    )
                page_provenance[field] = value
            raw_hash = page_provenance["raw_response_sha256"]
            if raw_hash is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", raw_hash):
                raise DataLakeError(
                    f"legacy candle source raw_response_sha256 is invalid: {path}[{index}]"
                )
            key = _source_key(page_provenance)
            source_page_id = source_ids.get(key)
            if source_page_id is None:
                source_page_id = len(sources)
                source_ids[key] = source_page_id
                sources.append(
                    {
                        "source_page_id": source_page_id,
                        **page_provenance,
                        "first_begin": begin,
                        "last_begin": begin,
                        "row_count": 0,
                    }
                )
            page = sources[source_page_id]
            page["last_begin"] = begin
            page["row_count"] += 1
        bars.append(
            {
                "begin": begin,
                "end": end,
                "open": _number(row.get("open"), "open"),
                "high": _number(row.get("high"), "high"),
                "low": _number(row.get("low"), "low"),
                "close": close,
                "value": _number(row.get("value"), "value"),
                "volume": _integer(row.get("volume"), "volume"),
                "source_page_id": source_page_id,
            }
        )
    if any(left["begin"] >= right["begin"] for left, right in zip(bars, bars[1:])):
        raise DataLakeError(f"legacy candle timestamps are not strictly increasing: {path}")
    row_semantics = {
        "price_proxy_kind": semantic_values["price_proxy_kind"],
        "price_proxy_assumption": semantic_values["price_proxy_assumption"],
        "price_proxy_matches_close": True,
    }
    return LegacyCandleSeries(
        path=resolved,
        sha256=hashlib.sha256(raw).hexdigest(),
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
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
        pq.write_table(table, temporary, compression="zstd", version="2.6")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class RawResponseArchive:
    """Retention-managed exact ISS response bodies, compressed as ``.json.zst``."""

    def __init__(self, lake_root: Path):
        self.lake_root = lake_root.resolve()
        self.root = self.lake_root / "raw"
        self.manifest_path = self.root / "manifest.json"
        self._codec = pa.Codec("zstd")

    def _read_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            return {"schema_version": RAW_ARCHIVE_SCHEMA, "entries": []}
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataLakeError(f"cannot read raw response manifest: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != RAW_ARCHIVE_SCHEMA:
            raise DataLakeError("unsupported raw response manifest schema")
        if not isinstance(value.get("entries"), list):
            raise DataLakeError("raw response manifest entries must be a list")
        return value

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _atomic_write_json(self.manifest_path, manifest)

    @staticmethod
    def _entry_path(entry: dict[str, Any]) -> str:
        value = entry.get("path")
        if (
            not isinstance(value, str)
            or Path(value).name != value
            or not value.endswith(".json.zst")
        ):
            raise DataLakeError("raw response manifest contains an unsafe path")
        return value

    def archive(
        self,
        *,
        url: str,
        raw: bytes,
        received_at: str,
        ttl_days: int = 30,
        pin: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url or not isinstance(raw, bytes):
            raise DataLakeError("raw response requires a URL and exact bytes")
        if ttl_days < 0:
            raise DataLakeError("raw response TTL must not be negative")
        now = datetime.now(UTC)
        request_sha256 = hashlib.sha256(url.encode("utf-8")).hexdigest()
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        received_sha256 = hashlib.sha256(received_at.encode("utf-8")).hexdigest()
        # The tuple request hash, response hash and observed receipt time is the
        # identity; two identical bodies observed at different times remain auditable.
        entry_id = f"{request_sha256}-{raw_sha256}-{received_sha256}"
        filename = f"{entry_id}.json.zst"
        compressed = self._codec.compress(raw).to_pybytes()
        _atomic_write_bytes(self.root / filename, compressed)
        manifest = self._read_manifest()
        old = next(
            (
                item
                for item in manifest["entries"]
                if isinstance(item, dict) and item.get("id") == entry_id
            ),
            None,
        )
        pinned = bool(pin or (old and old.get("pinned") is True))
        stored_at = now.isoformat(timespec="seconds")
        entry = {
            "id": entry_id,
            "path": filename,
            "request_sha256": request_sha256,
            "raw_response_sha256": raw_sha256,
            "received_at_sha256": received_sha256,
            "url": url,
            "received_at": received_at,
            "stored_at": stored_at,
            "last_seen_at": stored_at,
            "expires_at": None
            if pinned
            else (now + timedelta(days=ttl_days)).isoformat(timespec="seconds"),
            "pinned": pinned,
            "raw_bytes": len(raw),
            "compressed_bytes": len(compressed),
        }
        entries = [
            item
            for item in manifest["entries"]
            if not (isinstance(item, dict) and item.get("id") == entry_id)
        ]
        entries.append(entry)
        manifest["entries"] = sorted(entries, key=lambda item: item["id"])
        self._write_manifest(manifest)
        return entry

    def pin(self, entry_id: str, *, pinned: bool = True) -> dict[str, Any]:
        manifest = self._read_manifest()
        for entry in manifest["entries"]:
            if isinstance(entry, dict) and entry.get("id") == entry_id:
                entry["pinned"] = pinned
                entry["expires_at"] = (
                    None
                    if pinned
                    else entry.get("expires_at") or datetime.now(UTC).isoformat(timespec="seconds")
                )
                self._write_manifest(manifest)
                return entry
        raise DataLakeError(f"raw response entry is not registered: {entry_id}")

    def verify(self) -> dict[str, Any]:
        manifest = self._read_manifest()
        entries = [item for item in manifest["entries"] if isinstance(item, dict)]
        verified_bytes = 0
        registered_paths: set[str] = set()
        for entry in entries:
            name = self._entry_path(entry)
            registered_paths.add(name)
            try:
                raw = self._codec.decompress(
                    (self.root / name).read_bytes(), int(entry["raw_bytes"])
                )
            except (OSError, ValueError, KeyError) as exc:
                raise DataLakeError(f"cannot verify raw response {entry.get('id')}: {exc}") from exc
            url = entry.get("url")
            received_at = entry.get("received_at")
            if not isinstance(url, str) or not isinstance(received_at, str):
                raise DataLakeError(f"raw response provenance is invalid: {entry.get('id')}")
            request_sha256 = hashlib.sha256(url.encode("utf-8")).hexdigest()
            received_sha256 = hashlib.sha256(received_at.encode("utf-8")).hexdigest()
            expected_id = f"{request_sha256}-{entry.get('raw_response_sha256')}-{received_sha256}"
            if (
                entry.get("request_sha256") != request_sha256
                or entry.get("received_at_sha256") != received_sha256
                or entry.get("id") != expected_id
            ):
                raise DataLakeError(
                    f"raw response provenance hash differs from manifest: {entry.get('id')}"
                )
            if hashlib.sha256(raw).hexdigest() != entry.get("raw_response_sha256"):
                raise DataLakeError(
                    f"raw response SHA-256 differs from manifest: {entry.get('id')}"
                )
            verified_bytes += len(raw)
        orphans = sorted(
            path.name for path in self.root.glob("*.json.zst") if path.name not in registered_paths
        )
        return {
            "entry_count": len(entries),
            "verified_raw_bytes": verified_bytes,
            "orphan_files": orphans,
        }

    def gc(self, *, dry_run: bool = True) -> dict[str, Any]:
        manifest = self._read_manifest()
        now = datetime.now(UTC)
        expired: list[dict[str, Any]] = []
        keep: list[dict[str, Any]] = []
        for entry in manifest["entries"]:
            if not isinstance(entry, dict):
                raise DataLakeError("raw response manifest entry is invalid")
            expires_at = entry.get("expires_at")
            is_expired = False
            if entry.get("pinned") is not True and isinstance(expires_at, str):
                try:
                    is_expired = datetime.fromisoformat(expires_at).astimezone(UTC) <= now
                except ValueError:
                    raise DataLakeError(f"raw response expiry is invalid: {entry.get('id')}")
            (expired if is_expired else keep).append(entry)
        registered_paths = {self._entry_path(entry) for entry in manifest["entries"]}
        orphans = sorted(
            path for path in self.root.glob("*.json.zst") if path.name not in registered_paths
        )
        planned = [self._entry_path(entry) for entry in expired] + [path.name for path in orphans]
        if not dry_run:
            for name in planned:
                (self.root / name).unlink(missing_ok=True)
            manifest["entries"] = keep
            self._write_manifest(manifest)
        return {
            "status": "dry_run" if dry_run else "collected",
            "expired_entry_count": len(expired),
            "orphan_file_count": len(orphans),
            "planned_files": sorted(planned),
            "retained_entry_count": len(keep),
        }


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
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataLakeError(f"cannot read data-lake manifest: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != DATA_LAKE_SCHEMA:
            raise DataLakeError("unsupported data-lake manifest schema")
        if not isinstance(value.get("datasets"), list):
            raise DataLakeError("data-lake manifest datasets must be a list")
        by_id: dict[str, dict[str, Any]] = {}
        by_legacy: dict[Path, dict[str, Any]] = {}
        for record in value["datasets"]:
            if not isinstance(record, dict):
                continue
            ds_id = record.get("dataset_id")
            if isinstance(ds_id, str):
                by_id[ds_id] = record
            lp = record.get("legacy_path")
            if isinstance(lp, str):
                try:
                    by_legacy[self._record_path(lp).resolve()] = record
                except OSError:
                    pass
        self._manifest_sig = sig
        self._manifest_cache = value
        self._manifest_by_id = by_id
        self._manifest_by_legacy = by_legacy
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
            connection.execute(
                "DELETE FROM source_pages WHERE dataset_id = ?", [record["dataset_id"]]
            )
            connection.execute(
                "DELETE FROM bar_datasets WHERE dataset_id = ?", [record["dataset_id"]]
            )
            connection.execute(
                """
                INSERT INTO bar_datasets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                [
                    record[key]
                    for key in (
                        "dataset_id",
                        "secid",
                        "board",
                        "interval",
                        "timeframe",
                        "bars_path",
                        "sources_path",
                        "row_count",
                        "first_begin",
                        "last_begin",
                        "legacy_path",
                        "legacy_sha256",
                        "parquet_sha256",
                        "sources_sha256",
                        "created_at",
                    )
                ],
            )
            if sources:
                connection.executemany(
                    """
                    INSERT INTO source_pages VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    [
                        [
                            record["dataset_id"],
                            *[
                                source[key]
                                for key in (
                                    "source_page_id",
                                    "url",
                                    "received_at",
                                    "raw_response_sha256",
                                    "first_begin",
                                    "last_begin",
                                    "row_count",
                                )
                            ],
                        ]
                        for source in sources
                    ],
                )

    def _upsert_manifest(self, record: dict[str, Any]) -> None:
        manifest = self._read_manifest()
        previous = next(
            (
                entry
                for entry in manifest["datasets"]
                if isinstance(entry, dict) and entry.get("dataset_id") == record["dataset_id"]
            ),
            None,
        )
        if previous is not None and previous != record:
            revisions = manifest.setdefault("dataset_revisions", [])
            if not isinstance(revisions, list):
                raise DataLakeError("data-lake manifest dataset_revisions must be a list")
            revisions.append(
                {
                    "dataset_id": record["dataset_id"],
                    "replaced_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "record": previous,
                }
            )
        entries = [
            entry
            for entry in manifest["datasets"]
            if entry.get("dataset_id") != record["dataset_id"]
        ]
        entries.append(record)
        manifest["datasets"] = sorted(entries, key=lambda item: item["dataset_id"])
        manifest["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _atomic_write_json(self.manifest_path, manifest)

    def import_legacy(self, legacy_path: Path, *, replace: bool = False) -> dict[str, Any]:
        series = _read_legacy_series(legacy_path)
        bars_path, sources_path = self._paths(series)
        reject_sealed_table_write(self.root, bars_path)
        if not replace and (bars_path.exists() or sources_path.exists()):
            raise DataLakeError(
                f"data-lake dataset already exists: {series.dataset_id}; use --replace"
            )
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
            "legacy_path": str(series.path.relative_to(self.root))
            if series.path.is_relative_to(self.root)
            else str(series.path),
            "legacy_sha256": series.sha256,
            "legacy_metadata": series.legacy_metadata,
            "row_semantics": series.row_semantics,
            "bars_path": str(bars_path.relative_to(self.root))
            if bars_path.is_relative_to(self.root)
            else str(bars_path),
            "sources_path": str(sources_path.relative_to(self.root))
            if sources_path.is_relative_to(self.root)
            else str(sources_path),
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
        self._read_manifest()
        record = self._manifest_by_id.get(dataset_id)
        if record is not None:
            return record
        raise DataLakeError(f"data-lake dataset is not registered: {dataset_id}")

    def _record_path(self, value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else self.root / candidate

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
        """Read a registered Parquet dataset as rows consumed by research logic."""
        record = self._record(secid, timeframe)
        bars_path = self._record_path(record["bars_path"])
        table = pq.ParquetFile(bars_path).read(
            columns=list(columns) if columns is not None else None
        )
        semantics = record.get("row_semantics")
        price_proxy_kind = (
            semantics.get("price_proxy_kind") if isinstance(semantics, dict) else None
        )
        rows = []
        for row in table.to_pylist():
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
        table = pq.ParquetFile(bars_path).read(
            columns=list(columns) if columns is not None else None
        )
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
        if not legacy_path.exists():
            return verify_archived_record(self, legacy_path)
        series = _read_legacy_series(legacy_path)
        record = self._record(series.secid, series.timeframe)
        if record.get("legacy_sha256") != series.sha256:
            raise DataLakeError(
                f"legacy SHA-256 differs from imported dataset: {series.dataset_id}"
            )
        if record.get("legacy_metadata") != series.legacy_metadata:
            raise DataLakeError(
                f"legacy metadata differs from imported dataset: {series.dataset_id}"
            )
        if record.get("row_semantics") != series.row_semantics:
            raise DataLakeError(
                f"legacy row semantics differ from imported dataset: {series.dataset_id}"
            )
        bars_path = self.root / record["bars_path"]
        sources_path = self.root / record["sources_path"]
        if sha256_file(bars_path) != record.get("parquet_sha256"):
            raise DataLakeError(f"bars Parquet SHA-256 differs from manifest: {series.dataset_id}")
        if sha256_file(sources_path) != record.get("sources_sha256"):
            raise DataLakeError(
                f"sources Parquet SHA-256 differs from manifest: {series.dataset_id}"
            )
        actual_bars = pq.ParquetFile(bars_path).read().to_pylist()
        actual_sources = pq.ParquetFile(sources_path).read().to_pylist()
        if actual_bars != series.bars:
            raise DataLakeError(f"Parquet bars differ from legacy rows: {series.dataset_id}")
        if actual_sources != series.sources:
            raise DataLakeError(
                f"Parquet source manifest differs from legacy provenance: {series.dataset_id}"
            )
        with duckdb.connect(str(self.catalog_path), read_only=True) as connection:
            catalog = connection.execute(
                "SELECT row_count, legacy_sha256, parquet_sha256, sources_sha256 FROM bar_datasets WHERE dataset_id = ?",
                [series.dataset_id],
            ).fetchone()
        if catalog != (
            len(series.bars),
            series.sha256,
            record["parquet_sha256"],
            record["sources_sha256"],
        ):
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

    @property
    def raw_archive(self) -> RawResponseArchive:
        return RawResponseArchive(self.lake_root)

    def upsert_legacy(self, legacy_path: Path) -> dict[str, Any]:
        """Atomically replace a dataset from a merged legacy series without duplicate keys."""
        series = _read_legacy_series(legacy_path)
        try:
            self._record(series.secid, series.timeframe)
            replace = True
        except DataLakeError:
            replace = False
        return self.import_legacy(series.path, replace=replace)

    def verify_all(self) -> dict[str, Any]:
        records = [item for item in self._read_manifest()["datasets"] if isinstance(item, dict)]
        datasets = []
        for record in sorted(records, key=lambda item: str(item.get("dataset_id"))):
            legacy_path = self._record_path(str(record.get("legacy_path", "")))
            datasets.append(self.verify_legacy(legacy_path))
        return {
            "status": "verified",
            "dataset_count": len(datasets),
            "datasets": datasets,
            "raw": self.raw_archive.verify(),
        }

    def compact(self, *, force: bool = False) -> dict[str, Any]:
        """Keep each registered dataset as one Zstd bars file and one source file."""
        records = [item for item in self._read_manifest()["datasets"] if isinstance(item, dict)]
        results = []
        for record in sorted(records, key=lambda item: str(item.get("dataset_id"))):
            dataset_id = str(record.get("dataset_id"))
            if force:
                reject_sealed_table_write(self.root, self._record_path(record["bars_path"]))
            if not force:
                results.append({"dataset_id": dataset_id, "status": "already_compact"})
                continue
            bars_path = self._record_path(str(record["bars_path"]))
            sources_path = self._record_path(str(record["sources_path"]))
            bars = pq.ParquetFile(bars_path).read()
            sources = pq.ParquetFile(sources_path).read()
            _atomic_write_parquet(bars_path, bars)
            _atomic_write_parquet(sources_path, sources)
            updated = dict(record)
            updated["parquet_sha256"] = sha256_file(bars_path)
            updated["sources_sha256"] = sha256_file(sources_path)
            updated["compacted_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            self._upsert_catalog(updated, sources.to_pylist())
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


@dataclass(frozen=True)
class BarSeries:
    """Business-facing candle rows detached from their physical representation.

    ``source_path`` and ``source_sha256`` deliberately remain the selected legacy
    input identity. A verified Parquet mirror therefore preserves the existing
    run provenance and is only selected when it represents that exact JSON file.
    """

    schema_version: str
    secid: str | None
    interval: int | None
    timeframe: str | None
    rows: tuple[dict[str, Any], ...]
    source_path: str
    source_sha256: str
    backend: str
    cache_key: tuple[Any, ...]
    metadata: dict[str, Any]


class BarStore(Protocol):
    """Read the selected legacy input as a stable business-facing bar series."""

    def read(self, legacy_path: Path, *, columns: Iterable[str] | None = None) -> BarSeries:
        """Return a series for ``legacy_path`` without exposing storage details."""


class LegacyJsonBarStore:
    """Compatibility reader for existing ``iss-candles.v1`` files.

    It intentionally accepts the historical lightweight fixture shape too. The
    stricter ``_read_legacy_series`` remains the importer validation boundary.
    """

    def read(self, legacy_path: Path, *, columns: Iterable[str] | None = None) -> BarSeries:
        try:
            raw = legacy_path.read_bytes()
            payload = json.loads(raw)
            stat = legacy_path.stat()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataLakeError(f"cannot read legacy candle JSON: {legacy_path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != LEGACY_CANDLE_SCHEMA:
            raise DataLakeError(f"legacy file is not {LEGACY_CANDLE_SCHEMA}: {legacy_path}")
        raw_rows = payload.get("rows")
        if not isinstance(raw_rows, list):
            raise DataLakeError(f"legacy candle rows must be a list: {legacy_path}")
        if any(not isinstance(row, dict) for row in raw_rows):
            raise DataLakeError(f"legacy candle row is not an object: {legacy_path}")
        interval = payload.get("interval")
        if isinstance(interval, bool) or not isinstance(interval, int):
            interval = None
        secid = payload.get("secid") if isinstance(payload.get("secid"), str) else None
        timeframe = payload.get("timeframe") if isinstance(payload.get("timeframe"), str) else None
        return BarSeries(
            schema_version=LEGACY_CANDLE_SCHEMA,
            secid=secid,
            interval=interval,
            timeframe=timeframe,
            rows=tuple(
                (
                    {
                        **{key: row[key] for key in columns if key in row},
                        **(
                            {"price_proxy_kind": row["price_proxy_kind"]}
                            if "price_proxy_kind" in row
                            else {}
                        ),
                    }
                    if columns is not None
                    else dict(row)
                )
                for row in raw_rows
            ),
            source_path=str(legacy_path),
            source_sha256=hashlib.sha256(raw).hexdigest(),
            backend="legacy_json",
            cache_key=("legacy_json", str(legacy_path.resolve()), stat.st_mtime_ns, stat.st_size),
            metadata={key: value for key, value in payload.items() if key != "rows"},
        )


class MarketDataStore:
    """Route a selected JSON input to its verified Parquet mirror when safe.

    The source file stays the authority during Phase 3: a Parquet dataset is
    used only when its manifest names this exact file and its recorded legacy
    SHA-256 still matches. Otherwise callers transparently retain JSON behavior.
    """

    def __init__(self, root: Path, lake_root: Path | None = None):
        self.root = root.resolve()
        self.parquet = ParquetBarStore(self.root, lake_root)
        self.legacy = LegacyJsonBarStore()
        self.sealed_h1 = SealedH1Reader(self.root)
        self._cache: dict[tuple[Any, ...], BarSeries] = {}
        self._sha_cache: dict[tuple[str, int, int], str] = {}

    def _legacy_sha256(self, legacy_path: Path) -> str:
        resolved = legacy_path.resolve()
        st = resolved.stat()
        sig = (str(resolved), st.st_mtime_ns, st.st_size)
        cached = self._sha_cache.get(sig)
        if cached is not None:
            return cached
        digest = sha256_file(resolved)
        if len(self._sha_cache) > 4096:
            self._sha_cache.clear()
        self._sha_cache[sig] = digest
        return digest

    def select_h1(self, secid: str) -> Path:
        return self.sealed_h1.select(secid)

    def source_available(self, path: Path) -> bool:
        if path.name.endswith("_1h.json") and self.sealed_h1.payload() is not None:
            value = self.sealed_h1.payload()
            try:
                relative = path.absolute().relative_to(self.root).as_posix()
            except ValueError:
                return False
            if relative not in value["records"]:
                return False
            self.sealed_h1.read(path, columns=("begin",))
            return True
        return path.is_file()

    def _cache_key(
        self,
        legacy_path: Path,
        columns: tuple[str, ...] | None,
        record: dict[str, Any] | None,
    ) -> tuple[Any, ...] | None:
        try:
            stat = legacy_path.stat()
        except OSError:
            return None
        key: tuple[Any, ...] = (
            str(legacy_path.resolve()),
            stat.st_mtime_ns,
            stat.st_size,
            columns,
        )
        if record is None:
            return (*key, "legacy_json")
        try:
            bars_path = self.parquet._record_path(str(record["bars_path"]))
            bars_stat = bars_path.stat()
            manifest_stat = self.parquet.manifest_path.stat()
        except (KeyError, OSError):
            return (
                *key,
                "parquet_unavailable",
                record.get("legacy_sha256"),
                record.get("parquet_sha256"),
            )
        return (
            *key,
            "parquet",
            record.get("dataset_id"),
            record.get("legacy_sha256"),
            record.get("parquet_sha256"),
            manifest_stat.st_mtime_ns,
            manifest_stat.st_size,
            str(bars_path.resolve()),
            bars_stat.st_mtime_ns,
            bars_stat.st_size,
        )

    def read(self, legacy_path: Path, *, columns: Iterable[str] | None = None) -> BarSeries:
        requested_columns = tuple(columns) if columns is not None else None
        if legacy_path.name.endswith("_1h.json"):
            sealed = self.sealed_h1.read(legacy_path, columns=requested_columns)
            if sealed is not None:
                return sealed
        try:
            record = self.parquet.record_for_legacy_path(legacy_path)
        except DataLakeError:
            # A damaged or absent lake must never displace the retained legacy input.
            record = None
        key = self._cache_key(legacy_path, requested_columns, record)
        if key is not None and key in self._cache:
            return self._cache[key]
        if record is not None and self._legacy_sha256(legacy_path) == record.get("legacy_sha256"):
            try:
                series = self.parquet.read_series(
                    str(record["secid"]),
                    str(record["timeframe"]),
                    columns=requested_columns,
                    source_path=legacy_path,
                    source_sha256=str(record["legacy_sha256"]),
                )
            except (DataLakeError, OSError, KeyError):
                series = self.legacy.read(legacy_path, columns=requested_columns)
        else:
            series = self.legacy.read(legacy_path, columns=requested_columns)
        if key is not None:
            self._cache[key] = series
        return series
