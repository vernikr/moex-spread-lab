"""Route a selected JSON input to its verified Parquet mirror or sealed H1 dataset."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path
from threading import Lock
from typing import Any

from moex_spread_lab.core.w0 import sha256_file

from .bar_types import BarSeries, DataLakeError, SealedH1Reader
from .legacy_series import LegacyJsonBarStore
from .parquet_store import ParquetBarStore


class MarketDataStore:
    """Route a selected JSON input to its verified Parquet mirror when safe.

    The source file stays the authority during Phase 3: a Parquet dataset is
    used only when its manifest names this exact file and its recorded legacy
    SHA-256 still matches. Otherwise callers transparently retain JSON behavior.
    """

    def __init__(self, root: Path, lake_root: Path | None = None) -> None:
        self.root = root.resolve()
        self.parquet = ParquetBarStore(self.root, lake_root)
        self.legacy = LegacyJsonBarStore()
        self.sealed_h1 = SealedH1Reader(self.root)
        self._cache: OrderedDict[tuple[Any, ...], BarSeries] = OrderedDict()
        self._cache_lock = Lock()
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
        value = self.sealed_h1.payload()
        if path.name.endswith("_1h.json") and value is not None:
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
            bars_path = self.parquet.record_path(str(record["bars_path"]))
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

    def _read_unsealed(
        self,
        legacy_path: Path,
        requested_columns: tuple[str, ...] | None,
        record: dict[str, Any] | None,
    ) -> BarSeries:
        if record is not None and self._legacy_sha256(legacy_path) == record.get("legacy_sha256"):
            try:
                return self.parquet.read_series(
                    str(record["secid"]),
                    str(record["timeframe"]),
                    columns=requested_columns,
                    source_path=legacy_path,
                    source_sha256=str(record["legacy_sha256"]),
                )
            except (DataLakeError, OSError, KeyError):
                return self.legacy.read(legacy_path, columns=requested_columns)
        return self.legacy.read(legacy_path, columns=requested_columns)

    def read(self, legacy_path: Path, *, columns: Iterable[str] | None = None) -> BarSeries:
        requested_columns = tuple(columns) if columns is not None else None
        if legacy_path.name.endswith("_1h.json"):
            sealed = self.sealed_h1.read(legacy_path, columns=requested_columns)
            if sealed is not None:
                return sealed
        try:
            record = self.parquet.record_for_legacy_path(legacy_path)
        except DataLakeError:
            record = None
        key = self._cache_key(legacy_path, requested_columns, record)
        if key is not None:
            with self._cache_lock:
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    return cached
        series = self._read_unsealed(legacy_path, requested_columns, record)
        if key is not None:
            with self._cache_lock:
                self._cache[key] = series
                self._cache.move_to_end(key)
                while len(self._cache) > 128:
                    self._cache.popitem(last=False)
        return series
