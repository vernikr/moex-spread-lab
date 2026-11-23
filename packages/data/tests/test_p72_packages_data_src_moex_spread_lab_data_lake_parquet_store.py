"""Isolated regressions for the strict-typed `ParquetBarStore` facade contract."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from moex_spread_lab.data.lake import legacy_series, parquet_catalog, parquet_store
from moex_spread_lab.data.lake.bar_types import DataLakeError
from moex_spread_lab.data.lake.parquet_store import ParquetBarStore


def _legacy(root: Path, name: str = "CNYRUBF_1h.json") -> Path:
    path = root / "data" / "w1" / "candles" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    source = {
        "url": "https://iss.moex.example/page-0",
        "received_at": "2026-11-20T10:00:00+00:00",
        "raw_response_sha256": "a" * 64,
    }
    rows = [
        {
            "begin": f"2026-01-01 {10 + index:02d}:00:00",
            "end": f"2026-01-01 {10 + index:02d}:59:59",
            "open": 10.0 + index,
            "high": 11.0 + index,
            "low": 9.5 + index,
            "close": 10.5 + index,
            "value": 12.0,
            "volume": 20 + index,
            "price_proxy": str(10.5 + index),
            "price_proxy_kind": "candle_close",
            "price_proxy_assumption": "test_proxy",
            "source": source,
        }
        for index in range(3)
    ]
    payload = {
        "schema_version": "iss-candles.v1",
        "secid": "CNYRUBF",
        "board": "RFUD",
        "interval": 60,
        "timeframe": "1h",
        "from": "2026-01-01",
        "till": "2026-01-02",
        "coverage_status": "available",
        "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
        "rows": rows,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _imported_store(directory: str) -> tuple[ParquetBarStore, Path]:
    root = Path(directory)
    legacy = _legacy(root)
    store = ParquetBarStore(root)
    store.import_legacy(legacy)
    return store, legacy


def test_p72_packages_data_src_moex_spread_lab_data_lake_parquet_store_facade_identity() -> None:
    # The strict pass must not move the private hooks that `data_lake` re-exports and
    # `h1_migration`/`bar_types` reach through the store.
    assert parquet_store._component is legacy_series._component
    assert parquet_store._read_legacy_series is legacy_series._read_legacy_series
    assert parquet_store._atomic_write_parquet is parquet_catalog._atomic_write_parquet
    assert parquet_store.__all__ == [
        "BAR_DATASET_SCHEMA",
        "BAR_SCHEMA",
        "DATA_LAKE_SCHEMA",
        "SOURCE_SCHEMA",
        "ParquetBarStore",
        "_atomic_write_parquet",
    ]
    assert parquet_store.BAR_SCHEMA is parquet_catalog.BAR_SCHEMA
    assert ParquetBarStore.record_path is not None


def test_p72_packages_data_src_moex_spread_lab_data_lake_parquet_store_read_series_semantics(
    tmp_path: Path,
) -> None:
    store, legacy = _imported_store(str(tmp_path))
    series = store.read_series("CNYRUBF", "1h", source_path=legacy, source_sha256="given")
    assert series.schema_version == "iss-candles.v1"
    assert series.backend == "parquet"
    assert series.source_path == str(legacy)
    assert series.source_sha256 == "given"
    assert series.secid == "CNYRUBF"
    assert series.timeframe == "1h"
    assert series.interval == 60
    assert len(series.rows) == 3
    assert all(row["price_proxy_kind"] == "candle_close" for row in series.rows)
    assert series.cache_key[0] == "parquet"
    assert series.metadata["dataset_id"] == "1h/CNYRUBF"


def test_p72_packages_data_src_moex_spread_lab_data_lake_parquet_store_tolerates_malformed_semantics(
    tmp_path: Path,
) -> None:
    store, _legacy_path = _imported_store(str(tmp_path))
    manifest_path = tmp_path / "data-lake" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["datasets"][0]["row_semantics"] = "candle_close"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    time.sleep(0.01)
    series = store.read_series("CNYRUBF", "1h")
    assert all("price_proxy_kind" not in row for row in series.rows)


def test_p72_packages_data_src_moex_spread_lab_data_lake_parquet_store_read_bars_filters(
    tmp_path: Path,
) -> None:
    store, _legacy_path = _imported_store(str(tmp_path))
    full = store.read_bars("CNYRUBF", "1h")
    assert full.num_rows == 3
    selected = store.read_bars(
        "CNYRUBF", "1h", columns=("begin", "close"), start="2026-01-01 11:00:00"
    )
    assert selected.to_pylist() == [
        {"begin": "2026-01-01 11:00:00", "close": 11.5},
        {"begin": "2026-01-01 12:00:00", "close": 12.5},
    ]
    try:
        store.read_bars("MISSING", "1h")
    except DataLakeError as exc:
        assert "MISSING" in str(exc)
    else:  # pragma: no cover - the store must reject unregistered datasets
        raise AssertionError("unregistered dataset was read")


def test_p72_packages_data_src_moex_spread_lab_data_lake_parquet_store_verify_and_compact_payloads(
    tmp_path: Path,
) -> None:
    store, legacy = _imported_store(str(tmp_path))
    assert store.verify_legacy(legacy)["status"] == "verified"
    archived = store.verify_all()
    assert archived["status"] == "verified"
    assert archived["dataset_count"] == 1
    assert archived["raw"]["entry_count"] == 0
    skipped = store.compact()
    assert skipped == {
        "status": "already_compact",
        "dataset_count": 1,
        "datasets": [{"dataset_id": "1h/CNYRUBF", "status": "already_compact"}],
    }
    compacted = store.compact(force=True)
    assert compacted["status"] == "compacted"
    assert compacted["datasets"][0]["row_count"] == 3
    manifest = json.loads((tmp_path / "data-lake" / "manifest.json").read_text(encoding="utf-8"))
    record: dict[str, Any] = manifest["datasets"][0]
    assert "compacted_at" in record
    assert ParquetBarStore(tmp_path).read_bars("CNYRUBF", "1h").num_rows == 3


def test_p72_packages_data_src_moex_spread_lab_data_lake_parquet_store_manifest_cache_signature(
    tmp_path: Path,
) -> None:
    store, _legacy_path = _imported_store(str(tmp_path))
    first = store._read_manifest()
    assert store._manifest_sig is not None
    assert store._read_manifest() is first
    manifest_path = tmp_path / "data-lake" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["datasets"][0]["legacy_metadata"]["coverage_status"] = "partial"
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    time.sleep(0.01)
    second = store._read_manifest()
    assert second is not first
    assert second["datasets"][0]["legacy_metadata"]["coverage_status"] == "partial"
    assert set(store._manifest_by_id) == {"1h/CNYRUBF"}
    manifest_path.unlink()
    cleared = store._read_manifest()
    assert cleared == {"schema_version": "market-data-lake.v1", "datasets": []}
    assert store._manifest_by_id == {}
    assert store._manifest_by_legacy == {}
