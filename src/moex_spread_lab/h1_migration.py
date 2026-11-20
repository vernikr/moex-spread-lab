"""Plan and verify isolated H1 candidate lakes; no activation or source deletion."""
from __future__ import annotations

import json
from pathlib import Path

from .data_lake import DataLakeError, ParquetBarStore, _read_legacy_series, _atomic_write_json
from .w0 import canonical_bytes, sha256_bytes, sha256_file

SCHEMA = "h1-source-plan.v1"
GROUPS = ("candles", "candles-screen")


def _source_files(root: Path):
    for group in GROUPS:
        folder = root / "data/w1" / group
        for parent in (root / "data", root / "data/w1", folder):
            if parent.is_symlink():
                raise DataLakeError(f"symlink source directory: {parent}")
        if folder.exists() and not folder.is_dir():
            raise DataLakeError(f"source directory is not a directory: {folder}")
        for path in sorted(folder.glob("*_1h.json")):
            if path.is_symlink() or not path.is_file():
                raise DataLakeError(f"non-regular H1 source: {path}")
            yield group, path


def build_h1_plan(root: Path) -> dict:
    root = root.resolve()
    files = []
    for group, path in _source_files(root):
        series = _read_legacy_series(path)
        if (series.timeframe != "1h" or series.interval != 60
                or path.name != f"{series.secid}_1h.json"):
            raise DataLakeError(f"H1 source identity disagrees with filename/interval: {path}")
        if sha256_file(path) != series.sha256:
            raise DataLakeError(f"source changed while planning: {path}")
        files.append({"source_group": group, "legacy_path": path.relative_to(root).as_posix(),
                      "secid": series.secid, "dataset_id": series.dataset_id,
                      "legacy_sha256": series.sha256, "logical_bytes": path.stat().st_size,
                      "row_count": len(series.bars), "first_begin": series.bars[0]["begin"],
                      "last_begin": series.bars[-1]["begin"], "source_page_count": len(series.sources)})
    if not files:
        raise DataLakeError("no runtime-named H1 source files found")
    files.sort(key=lambda e: e["legacy_path"])
    by_secid = {}
    for entry in files:
        by_secid.setdefault(entry["secid"], {})[entry["source_group"]] = entry["legacy_path"]
    selection = {secid: paths.get("candles-screen", paths.get("candles"))
                 for secid, paths in sorted(by_secid.items())}
    content = {"files": files, "screen_selection": selection}
    return {"schema_version": SCHEMA, "root": str(root), **content,
            "source_fingerprint": sha256_bytes(canonical_bytes(content)),
            "source_count": len(files), "selected_count": len(selection),
            "logical_bytes": sum(e["logical_bytes"] for e in files)}


def write_h1_plan(root: Path, path: Path) -> dict:
    result = build_h1_plan(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return result


def _load_current_plan(root: Path, plan_path: Path) -> dict:
    supplied = json.loads(plan_path.read_text())
    # Re-enumerate, not just rehash supplied entries: a new screened source changes priority.
    current = build_h1_plan(root)
    if supplied != current:
        raise DataLakeError("stale or altered H1 plan; regenerate and review source selection")
    return current


def _candidate_path(root: Path, output: Path) -> Path:
    output = output.absolute()
    if output.resolve().is_relative_to((root / "data").resolve()):
        raise DataLakeError("candidate lake must be outside data/")
    return output


def _verify_files(root: Path, plan: dict, output: Path) -> list[dict]:
    verified = []
    for group in GROUPS:
        store = ParquetBarStore(root, output / group)
        expected = {e["legacy_path"] for e in plan["files"] if e["source_group"] == group}
        records = store._read_manifest()["datasets"]
        if len(records) != len(expected) or {r["legacy_path"] for r in records} != expected:
            raise DataLakeError(f"candidate lake membership differs from plan: {group}")
        for entry in (e for e in plan["files"] if e["source_group"] == group):
            source = root / entry["legacy_path"]
            record = store.record_for_legacy_path(source)
            if record is None or record["legacy_sha256"] != entry["legacy_sha256"]:
                raise DataLakeError(f"candidate source identity mismatch: {source}")
            result = store.verify_legacy(source)
            for field in ("row_count", "first_begin", "last_begin", "source_page_count"):
                if record[field] != entry[field]:
                    raise DataLakeError(f"candidate metadata mismatch: {field}")
            verified.append({"source_group": group, "legacy_path": entry["legacy_path"],
                             **result, "first_begin": record["first_begin"],
                             "last_begin": record["last_begin"], "sources_sha256": record["sources_sha256"]})
    return verified


def stage_h1(root: Path, plan_path: Path, output: Path) -> dict:
    root = root.resolve()
    output = _candidate_path(root, output)
    plan = _load_current_plan(root, plan_path)
    # Reserve a new batch directory. Incomplete batches remain explicitly failed,
    # never discoverable by the production MarketDataStore and never resumed implicitly.
    output.mkdir(parents=True, exist_ok=False)
    report_path = output / "h1-stage.json"
    report = {"schema_version": "h1-candidate-stage.v1", "status": "staging",
              "source_fingerprint": plan["source_fingerprint"], "plan": plan,
              "runtime_activated": False, "source_deletion_allowed": False, "datasets": []}
    _atomic_write_json(report_path, report)
    try:
        for entry in plan["files"]:
            source = root / entry["legacy_path"]
            if sha256_file(source) != entry["legacy_sha256"]:
                raise DataLakeError(f"source changed before import: {source}")
            store = ParquetBarStore(root, output / entry["source_group"])
            record = store.import_legacy(source)
            if record["legacy_sha256"] != entry["legacy_sha256"]:
                raise DataLakeError(f"source changed during import: {source}")
            store.verify_legacy(source)
            report["datasets"].append({"source_group": entry["source_group"],
                                       "legacy_path": entry["legacy_path"], "dataset_id": record["dataset_id"]})
            _atomic_write_json(report_path, report)
        report["datasets"] = _verify_files(root, plan, output)
        _load_current_plan(root, plan_path)
        parquet = list(output.rglob("*.parquet"))
        report.update(status="verified_candidate", parquet_logical_bytes=sum(p.stat().st_size for p in parquet),
                      parquet_allocated_bytes=sum(p.stat().st_blocks * 512 for p in parquet))
        _atomic_write_json(report_path, report)
    except Exception as exc:
        report.update(status="failed", error=str(exc))
        _atomic_write_json(report_path, report)
        raise
    return report


def verify_h1_stage(root: Path, plan_path: Path, output: Path) -> dict:
    root = root.resolve()
    plan = _load_current_plan(root, plan_path)
    output = _candidate_path(root, output)
    report = json.loads((output / "h1-stage.json").read_text())
    if (report.get("schema_version") != "h1-candidate-stage.v1"
            or report.get("status") != "verified_candidate" or report.get("plan") != plan
            or report.get("source_fingerprint") != plan["source_fingerprint"]):
        raise DataLakeError("candidate stage is incomplete or belongs to another source plan")
    actual = _verify_files(root, plan, output)
    if actual != report["datasets"]:
        raise DataLakeError("candidate verification differs from recorded stage evidence")
    return {"status": "verified_candidate", "source_count": len(actual),
            "selected_count": plan["selected_count"], "source_fingerprint": plan["source_fingerprint"],
            "runtime_activated": False, "source_deletion_allowed": False}
