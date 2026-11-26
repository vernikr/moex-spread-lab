import hashlib
import json

from moex_spread_lab.data.retention.data_inventory import (
    DATA_INVENTORY_SCHEMA,
    build_data_inventory,
)


def write_json(root, path, value):
    file = root / "data" / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(value), encoding="utf-8")
    return file


def test_inventory_classifies_inputs_caches_and_candidates(tmp_path):
    files = {
        "w1/candles/CNYRUBF_1h.json": {
            "schema_version": "iss-candles.v1",
            "timeframe": "1h",
            "from": "2026-01-01",
            "till": "2026-01-02",
            "row_count": 2,
            "rows": [],
        },
        "w1/candles/CNYRUBF_10m.json": {"timeframe": "10m", "rows": []},
        "w1/candles-cache/CNYRUBF/60/page-000000.json": {"rows": []},
        "w1/candles-screen/X.json": {},
        "w1/candles-common/CNYRUBF_1m.json": {"timeframe": "1m", "rows": []},
        "w1/current-intake.json": {"schema_version": "iss-intake.v1"},
        "analytics/cny-diagnostic.json": {"schema_version": "analytical-baseline.v1", "daily": []},
    }
    paths = {path: write_json(tmp_path, path, value) for path, value in files.items()}
    (tmp_path / "data/other.bin").write_bytes(b"unknown")
    result = build_data_inventory(tmp_path, include_hashes=True)
    entries = {entry["path"]: entry for entry in result["files"]}
    assert result["schema_version"] == DATA_INVENTORY_SCHEMA
    assert {path: entry["classification"] for path, entry in entries.items()} == {
        "w1/candles/CNYRUBF_1h.json": "active_input",
        "w1/candles/CNYRUBF_10m.json": "archive_candidate",
        "w1/candles-cache/CNYRUBF/60/page-000000.json": "rebuildable_cache",
        "w1/candles-screen/X.json": "active_input",
        "w1/candles-common/CNYRUBF_1m.json": "archive_candidate",
        "w1/current-intake.json": "active_input",
        "analytics/cny-diagnostic.json": "derived_archive_candidate",
        "other.bin": "unclassified",
    }
    entry = entries["w1/candles/CNYRUBF_1h.json"]
    assert entry["metadata"]["row_count"] == 2
    assert entry["metadata"]["time_range"] == {"from": "2026-01-01", "till": "2026-01-02"}
    assert entry["sha256"] == hashlib.sha256(paths[entry["path"]].read_bytes()).hexdigest()
    summary = result["summary"]
    assert summary["file_count"] == len(entries)
    assert summary["logical_bytes"] == sum(entry["logical_bytes"] for entry in entries.values())
    assert summary["by_classification"]["active_input"]["file_count"] == 3
    assert summary["candidate_reclaimable_bytes"] > 0
    assert result["summary"]["candidate_reclaimable_requires_review"]


def test_fast_inventory_and_missing_data(tmp_path):
    missing = build_data_inventory(tmp_path)["summary"]
    assert not missing["data_root_exists"]
    assert missing["file_count"] == missing["logical_bytes"] == missing["allocated_bytes"] == 0
    assert missing["by_classification"] == {}
    (tmp_path / "data").mkdir()
    assert build_data_inventory(tmp_path)["summary"] == {**missing, "data_root_exists": True}
    write_json(tmp_path, "w1/candles/CNYRUBF_1h.json", {"timeframe": "1h", "rows": []})
    fast = build_data_inventory(tmp_path, inspect_json=False)
    assert fast["files"][0]["classification"] == "active_input"
    assert "metadata" not in fast["files"][0] and not fast["json_inspection"]
