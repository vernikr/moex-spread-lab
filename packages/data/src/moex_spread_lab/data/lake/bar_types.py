"""Shared bar-series types and the read-side H1 seal guard."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from typing import cast as _cast  # Narrow partially unknown pyarrow-stub callables.

import pyarrow.parquet as pq

from moex_spread_lab.core.w0 import W0Error, canonical_bytes, sha256_bytes, sha256_file

LEGACY_CANDLE_SCHEMA = "iss-candles.v1"
H1_SEAL_SCHEMA = "h1-sealed-read.v1"


def _validate_identity(relative: str, record: dict[str, Any]) -> None:
    p = Path(relative)
    if (
        p.is_absolute()
        or ".." in p.parts
        or len(p.parts) != 4
        or p.parts[:2] != ("data", "w1")
        or p.parts[2] not in ("candles", "candles-screen")
        or record["legacy_path"] != relative
        or record["timeframe"] != "1h"
    ):
        raise DataLakeError("unsafe H1 seal source identity")


def _check_symlink_ancestors(root: Path, legacy_path: Path) -> None:
    for p in (legacy_path, *legacy_path.parents):
        if p == root:
            break
        if p.is_symlink():
            raise DataLakeError("symlink H1 source is not allowed")


def _verify_sealed_hashes(
    legacy_path: Path,
    legacy_signature: tuple[int, int, int, int] | None,
    bars: Path,
    sources: Path,
    record: dict[str, Any],
) -> None:
    if legacy_signature is not None and sha256_file(legacy_path) != record["legacy_sha256"]:
        raise DataLakeError("retained H1 JSON changed after sealing; refusing stale Parquet")
    if (
        sha256_file(bars) != record["parquet_sha256"]
        or sha256_file(sources) != record["sources_sha256"]
    ):
        raise DataLakeError("sealed H1 table hash mismatch; refusing fallback")


class DataLakeError(W0Error):
    """Raised when an on-disk candle dataset cannot pass the data-lake contract."""


@dataclass(frozen=True)
class BarSeries:
    """Business-facing candle rows detached from their physical representation."""

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
        ...


def _signature(path: Path) -> tuple[int, int, int, int]:
    st = path.stat()
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _validate_seal_payload(root: Path, value: dict[str, Any], marker: dict[str, Any]) -> None:
    if (
        value.get("schema_version") != H1_SEAL_SCHEMA
        or value.get("root") != str(root)
        or marker.get("schema_version") != H1_SEAL_SCHEMA
        or sha256_bytes(canonical_bytes(value)) != marker.get("seal_sha256")
    ):
        raise DataLakeError("H1 seal integrity mismatch; refusing fallback")
    plan = value["plan"]
    expected = {e["legacy_path"] for e in plan["files"]}
    if set(value["records"]) != expected:
        raise DataLakeError("H1 seal membership mismatch")
    for relative, record in value["records"].items():
        _validate_identity(relative, record)
    if any(p not in expected for p in plan["screen_selection"].values()):
        raise DataLakeError("invalid H1 seal selection")


def _resolve_sealed_table_paths(root: Path, record: dict[str, Any]) -> tuple[Path, Path]:
    bars = root / record["bars_path"]
    sources = root / record["sources_path"]
    lake_root = root / "data-lake"
    for path in (bars, sources):
        if not path.resolve().is_relative_to(lake_root) or path.is_symlink():
            raise DataLakeError("sealed tables must remain inside data-lake")
    return bars, sources


def _load_sealed_bar_series(
    bars: Path,
    legacy_path: Path,
    record: dict[str, Any],
    key: tuple[Any, ...],
    columns: Iterable[str] | None,
) -> BarSeries:
    table = _cast(Any, pq.ParquetFile(bars).read)(
        columns=list(columns) if columns is not None else None
    )
    rows = table.to_pylist()
    proxy_kind = record.get("row_semantics", {}).get("price_proxy_kind")
    if proxy_kind is not None:
        for row in rows:
            row["price_proxy_kind"] = proxy_kind
    return BarSeries(
        schema_version=LEGACY_CANDLE_SCHEMA,
        secid=record["secid"],
        interval=60,
        timeframe="1h",
        rows=tuple(rows),
        source_path=str(legacy_path),
        source_sha256=record["legacy_sha256"],
        backend="sealed_parquet",
        cache_key=key,
        metadata=record,
    )


class SealedH1Reader:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.path = self.root / "data-lake/h1-seal.json"
        self.marker = self.root / "data-lake/h1-seal.enabled.json"
        self._snapshot: Any = None
        self._payload: dict[str, Any] | None = None
        self._cache: dict[tuple[Any, ...], BarSeries] = {}
        self._verified_sigs: set[tuple[Any, ...]] = set()

    def _clear_state(self) -> None:
        self._snapshot = self._payload = None
        self._cache.clear()
        self._verified_sigs.clear()

    def payload(self) -> dict[str, Any] | None:
        exists = [os.path.lexists(p) for p in (self.path, self.marker)]
        if not any(exists):
            self._clear_state()
            return None
        if not all(exists) or self.path.is_symlink() or self.marker.is_symlink():
            raise DataLakeError("H1 seal/activation marker missing or unsafe; refusing fallback")
        try:
            signature = (_signature(self.path), _signature(self.marker))
            if signature == self._snapshot:
                return self._payload
            value = json.loads(self.path.read_text(encoding="utf-8"))
            marker = json.loads(self.marker.read_text(encoding="utf-8"))
            _validate_seal_payload(self.root, value, marker)
            self._snapshot, self._payload = signature, value
            self._cache.clear()
            self._verified_sigs.clear()
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DataLakeError(f"cannot read H1 seal: {exc}") from exc

    def select(self, secid: str) -> Path:
        value = self.payload()
        screened = self.root / "data/w1/candles-screen" / f"{secid}_1h.json"
        intake = self.root / "data/w1/candles" / f"{secid}_1h.json"
        if value is None:
            return screened if screened.is_file() else intake
        relative = value["plan"]["screen_selection"].get(secid)
        if relative is None:
            return screened if screened.is_file() else intake
        chosen = self.root / relative
        if chosen != screened and os.path.lexists(screened):
            raise DataLakeError("new preferred screened H1 source requires a new seal")
        return chosen

    def _read_sealed(
        self, value: dict[str, Any], legacy_path: Path, columns: Iterable[str] | None
    ) -> BarSeries:
        relative = legacy_path.absolute().relative_to(self.root).as_posix()
        record = value["records"].get(relative)
        if record is None:
            raise DataLakeError(f"H1 input is not sealed: {relative}")
        _check_symlink_ancestors(self.root, legacy_path)
        self.select(record["secid"])
        bars, sources = _resolve_sealed_table_paths(self.root, record)
        legacy_sig = _signature(legacy_path) if os.path.lexists(legacy_path) else None
        bars_sig, sources_sig = _signature(bars), _signature(sources)
        col_key = tuple(columns) if columns is not None else None
        key = (relative, col_key, bars_sig, sources_sig, legacy_sig, self._snapshot)
        if key in self._cache:
            return self._cache[key]
        verify_key = (relative, bars_sig, sources_sig, legacy_sig, self._snapshot)
        if verify_key not in self._verified_sigs:
            _verify_sealed_hashes(legacy_path, legacy_sig, bars, sources, record)
            self._verified_sigs.add(verify_key)
        series = _load_sealed_bar_series(bars, legacy_path, record, key, columns)
        self._cache[key] = series
        return series

    def read(self, legacy_path: Path, *, columns: Iterable[str] | None = None) -> BarSeries | None:
        value = self.payload()
        if value is None:
            return None
        try:
            return self._read_sealed(value, legacy_path, columns)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DataLakeError(f"sealed H1 read failed: {exc}") from exc


def reject_sealed_table_write(root: Path, bars_path: Path) -> None:
    value = SealedH1Reader(root).payload()
    if value and any(
        (root / r["bars_path"]).resolve() == bars_path.resolve() for r in value["records"].values()
    ):
        raise DataLakeError(
            "cannot rewrite an active sealed table; restore JSON and deactivate first"
        )


def _check_archived_mirror_tables(
    store: Any, record: dict[str, Any], sealed: dict[str, Any]
) -> None:
    for path_key, hash_key in (
        ("bars_path", "parquet_sha256"),
        ("sources_path", "sources_sha256"),
    ):
        path = store.record_path(record[path_key])
        if sha256_file(path) != record[hash_key]:
            raise DataLakeError("archived mirror file hash mismatch")
        if not _cast(Any, pq.ParquetFile(path).read)().equals(
            _cast(Any, pq.ParquetFile(store.root / sealed[path_key]).read)()
        ):
            raise DataLakeError("archived mirror differs from sealed semantics")


def verify_archived_record(store: Any, legacy_path: Path) -> dict[str, Any]:
    """Verify retained pilot/candidate mirrors against sealed semantics after archival."""
    reader = SealedH1Reader(store.root)
    value = reader.payload()
    relative = legacy_path.absolute().relative_to(store.root).as_posix()
    if value is None or relative not in value["records"]:
        raise DataLakeError("missing legacy source without an exact verified H1 seal")
    sealed = value["records"][relative]
    reader.read(legacy_path, columns=("begin",))
    record = store.record_for_legacy_path(legacy_path)
    if record is None:
        raise DataLakeError("missing exact-path lake record")
    metadata_keys = (
        "dataset_id",
        "secid",
        "timeframe",
        "interval",
        "row_count",
        "first_begin",
        "last_begin",
        "legacy_sha256",
        "legacy_metadata",
        "row_semantics",
        "source_page_count",
    )
    for key in metadata_keys:
        if record.get(key) != sealed.get(key):
            raise DataLakeError(f"archived dataset metadata mismatch: {key}")
    _check_archived_mirror_tables(store, record, sealed)
    return {
        "status": "verified",
        "verification_basis": "sealed_h1",
        "dataset_id": record["dataset_id"],
        "row_count": record["row_count"],
        "legacy_sha256": record["legacy_sha256"],
    }


__all__ = [
    "H1_SEAL_SCHEMA",
    "LEGACY_CANDLE_SCHEMA",
    "BarSeries",
    "BarStore",
    "DataLakeError",
    "SealedH1Reader",
    "reject_sealed_table_write",
    "verify_archived_record",
]
