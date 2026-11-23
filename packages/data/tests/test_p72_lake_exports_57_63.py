"""Pin historical lake exports and dynamic guard hooks across strict typing."""

import json
from pathlib import Path

import pyarrow as pa
import pytest

from moex_spread_lab.data.lake import bar_types, data_lake, parquet_catalog


def test_public_aliases_keep_original_identity():
    assert bar_types.atomic_write_json is bar_types._atomic_write_json
    assert parquet_catalog._atomic_write_json is bar_types._atomic_write_json
    assert data_lake._atomic_write_json is bar_types._atomic_write_json
    assert parquet_catalog.atomic_write_parquet is parquet_catalog._atomic_write_parquet
    assert data_lake._atomic_write_parquet is parquet_catalog._atomic_write_parquet


def test_star_imports_preserve_preexisting_sets():
    expected = {
        "bar_types": {
            "Any",
            "BarSeries",
            "BarStore",
            "DataLakeError",
            "H1_SEAL_SCHEMA",
            "Iterable",
            "LEGACY_CANDLE_SCHEMA",
            "Path",
            "Protocol",
            "SealedH1Reader",
            "W0Error",
            "annotations",
            "canonical_bytes",
            "dataclass",
            "json",
            "os",
            "pq",
            "reject_sealed_table_write",
            "sha256_bytes",
            "sha256_file",
            "tempfile",
            "verify_archived_record",
        },
        "parquet_catalog": {
            "Any",
            "BAR_DATASET_SCHEMA",
            "BAR_SCHEMA",
            "DATA_LAKE_SCHEMA",
            "DataLakeError",
            "LegacyCandleSeries",
            "Path",
            "SOURCE_SCHEMA",
            "UTC",
            "annotations",
            "build_import_record",
            "datetime",
            "duckdb",
            "json",
            "os",
            "pa",
            "parse_lake_manifest",
            "pq",
            "sha256_file",
            "tempfile",
            "upsert_catalog",
            "upsert_manifest",
            "verify_dataset_against_legacy",
        },
    }
    for name, names in expected.items():
        scope = {}
        exec(f"from moex_spread_lab.data.lake.{name} import *", scope)
        assert set(scope) - {"__builtins__"} == names


def test_seal_helpers_use_late_bound_facade(monkeypatch, tmp_path):
    hashes = {tmp_path / "old": "old", tmp_path / "bars": "bars", tmp_path / "sources": "sources"}
    monkeypatch.setattr(bar_types, "sha256_file", hashes.__getitem__)
    record = {"legacy_sha256": "old", "parquet_sha256": "bars", "sources_sha256": "sources"}
    bar_types._verify_sealed_hashes(
        tmp_path / "old", (1, 1, 1, 1), tmp_path / "bars", tmp_path / "sources", record
    )
    record["parquet_sha256"] = "different"
    with pytest.raises(bar_types.DataLakeError, match="sealed H1 table hash mismatch"):
        bar_types._verify_sealed_hashes(
            tmp_path / "old", None, tmp_path / "bars", tmp_path / "sources", record
        )
    with pytest.raises(bar_types.DataLakeError, match="unsafe H1 seal source identity"):
        bar_types._validate_seal_record_identity("../bad", {})


def test_atomic_writes_and_failure_cleanup(monkeypatch, tmp_path):
    path = tmp_path / "nested" / "item.json"
    bar_types.atomic_write_json(path, {"я": 3})
    assert json.loads(path.read_text()) == {"я": 3}
    table_path = tmp_path / "nested" / "item.parquet"
    parquet_catalog.atomic_write_parquet(table_path, pa.table({"x": [1, 2]}))
    assert parquet_catalog.pq.read_table(table_path).to_pylist() == [{"x": 1}, {"x": 2}]

    def fail(*_args):
        raise OSError("replace failed")

    monkeypatch.setattr(parquet_catalog.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        parquet_catalog.atomic_write_parquet(tmp_path / "bad.parquet", pa.table({"x": [1]}))
    assert not list(tmp_path.glob(".bad.parquet.*.tmp"))


def test_manifest_validation_and_late_bound_writer(monkeypatch, tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("[1]")
    with pytest.raises(
        parquet_catalog.DataLakeError, match="unsupported data-lake manifest schema"
    ):
        parquet_catalog.parse_lake_manifest(path)
    path.write_text(
        json.dumps({"schema_version": parquet_catalog.DATA_LAKE_SCHEMA, "datasets": []})
    )
    manifest = parquet_catalog.parse_lake_manifest(path)
    calls = []
    monkeypatch.setattr(
        parquet_catalog, "_atomic_write_json", lambda target, data: calls.append((target, data))
    )
    parquet_catalog.upsert_manifest(Path(path), manifest, {"dataset_id": "one"})
    assert calls[0][0] == path
    assert calls[0][1]["datasets"] == [{"dataset_id": "one"}]
