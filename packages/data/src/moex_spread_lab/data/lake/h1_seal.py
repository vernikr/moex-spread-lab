"""Explicit, integrity-bound H1 activation; no source archival or deletion."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .bar_types import (
    H1_SEAL_SCHEMA,
    BarSeries,
    DataLakeError,
    SealedH1Reader,
    _signature,
    reject_sealed_table_write,
    verify_archived_record,
)
from moex_spread_lab.core.w0 import canonical_bytes, sha256_bytes
from .data_lake import ParquetBarStore
from .h1_migration import build_h1_plan, verify_h1_stage

SCHEMA = H1_SEAL_SCHEMA

__all__ = [
    "SCHEMA",
    "BarSeries",
    "DataLakeError",
    "SealedH1Reader",
    "_signature",
    "activate_h1",
    "deactivate_h1",
    "reject_sealed_table_write",
    "verify_archived_record",
    "verify_sealed_h1",
]


def _new_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def activate_h1(root: Path, plan_path: Path, candidate: Path) -> dict:
    root = root.resolve()
    reader = SealedH1Reader(root)
    if any(os.path.lexists(p) for p in (reader.path, reader.marker)):
        raise DataLakeError("H1 seal already exists; no implicit replacement")
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root / "data-lake"):
        raise DataLakeError("production H1 candidate must be inside data-lake")
    verify_h1_stage(root, plan_path, candidate)
    plan = json.loads(plan_path.read_text())
    records = {}
    for entry in plan["files"]:
        record = ParquetBarStore(root, candidate / entry["source_group"]).record_for_legacy_path(
            root / entry["legacy_path"]
        )
        if record is None:
            raise DataLakeError("missing candidate record")
        records[entry["legacy_path"]] = record
    value = {
        "schema_version": SCHEMA,
        "root": str(root),
        "plan": plan,
        "records": records,
        "source_deletion_allowed": False,
    }
    verify_h1_stage(root, plan_path, candidate)
    # Marker first: interrupted activation fails closed instead of silently falling back.
    _new_json(
        reader.marker,
        {"schema_version": SCHEMA, "seal_sha256": sha256_bytes(canonical_bytes(value))},
    )
    _new_json(reader.path, value)
    return verify_sealed_h1(root)


def verify_sealed_h1(root: Path) -> dict:
    reader = SealedH1Reader(root)
    value = reader.payload()
    if value is None:
        raise DataLakeError("no H1 seal is active")
    retained = 0
    for relative in value["records"]:
        path = reader.root / relative
        reader.read(path, columns=("begin", "close", "volume"))
        retained += int(path.is_file())
    return {
        "status": "sealed_verified",
        "dataset_count": len(value["records"]),
        "retained_json_count": retained,
        "source_fingerprint": value["plan"]["source_fingerprint"],
        "source_deletion_allowed": False,
    }


def deactivate_h1(root: Path) -> dict:
    reader = SealedH1Reader(root)
    value = reader.payload()
    if value is None:
        raise DataLakeError("no H1 seal is active")
    if build_h1_plan(reader.root) != value["plan"]:
        raise DataLakeError(
            "rollback requires all original JSON and exact selection restored"
        )
    # Preserve every activation's controls, including repeated rollback rehearsals.
    history = reader.root / "data-lake/h1-seal-history" / uuid.uuid4().hex
    history.mkdir(parents=True, exist_ok=False)
    for path in (reader.path, reader.marker):
        os.link(path, history / path.name)
    for path in (reader.path, reader.marker):
        path.unlink()
    return {
        "status": "deactivated",
        "restored_json_count": len(value["records"]),
        "evidence_dir": str(history),
    }
