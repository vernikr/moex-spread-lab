"""Gated archival/removal of the exact active H1 seal; never accepts free globs."""

from __future__ import annotations

import fcntl
import json
import os
import tarfile
import tempfile
import uuid
from pathlib import Path

import zstandard as zstd

from moex_spread_lab.core.w0 import canonical_bytes, sha256_bytes, sha256_file

from ..lake.data_lake import DataLakeError, _atomic_write_json
from ..lake.h1_migration import build_h1_plan
from ..lake.h1_seal import SealedH1Reader, verify_sealed_h1
from .data_archive_candidates import _json_new, _publish, verify_archive
from .data_retention import _prune_empty_parents, _safe_data_path, _validate_archive_path

SCHEMA = "sealed-h1-archive.v1"


def _evidence(path: Path, seal: dict):
    evidence = json.loads(path.read_text())
    if (
        evidence.get("source_fingerprint") != seal["plan"]["source_fingerprint"]
        or evidence.get("seal_sha256") != sha256_bytes(canonical_bytes(seal))
        or any(
            evidence.get("checks", {}).get(k) != "passed"
            for k in ("regression", "performance", "ui_smoke")
        )
    ):
        raise DataLakeError("missing, stale or failed cutover evidence")
    return evidence


def build_plan(root: Path, archive: Path, evidence: Path) -> dict:
    root = root.resolve()
    seal = SealedH1Reader(root).payload()
    if seal is None or build_h1_plan(root) != seal["plan"]:
        raise DataLakeError("all original H1 sources must match the active seal")
    verify_sealed_h1(root)
    _evidence(evidence, seal)
    archive = _validate_archive_path(root, archive)
    if archive.exists():
        raise DataLakeError("archive destination exists")
    return {
        "schema_version": SCHEMA,
        "root": str(root),
        "archive": str(archive),
        "seal_sha256": sha256_bytes(canonical_bytes(seal)),
        "source_fingerprint": seal["plan"]["source_fingerprint"],
        "evidence_path": str(evidence.resolve()),
        "evidence_sha256": sha256_file(evidence),
        "minimum_net_gain_fraction": 0.8,
        "files": [
            {
                "path": str(Path(e["legacy_path"]).relative_to("data")),
                "sha256": e["legacy_sha256"],
                "logical_bytes": e["logical_bytes"],
            }
            for e in seal["plan"]["files"]
        ],
    }


def _validate(plan: dict, root: Path):
    seal = SealedH1Reader(root).payload()
    if (
        plan.get("schema_version") != SCHEMA
        or plan.get("root") != str(root.resolve())
        or seal is None
        or plan.get("seal_sha256") != sha256_bytes(canonical_bytes(seal))
        or plan.get("source_fingerprint") != seal["plan"]["source_fingerprint"]
        or plan.get("minimum_net_gain_fraction") != 0.8
    ):
        raise DataLakeError("H1 archive plan does not match active seal")
    expected = [
        {
            "path": str(Path(e["legacy_path"]).relative_to("data")),
            "sha256": e["legacy_sha256"],
            "logical_bytes": e["logical_bytes"],
        }
        for e in seal["plan"]["files"]
    ]
    if plan.get("files") != expected or build_h1_plan(root) != seal["plan"]:
        raise DataLakeError("stale H1 sources or altered archive membership")
    evidence = Path(plan["evidence_path"])
    if sha256_file(evidence) != plan["evidence_sha256"]:
        raise DataLakeError("cutover evidence changed")
    _evidence(evidence, seal)
    _validate_archive_path(root, Path(plan["archive"]))
    verify_sealed_h1(root)


def _allocated(folder: Path):
    return sum(p.stat().st_blocks * 512 for p in folder.rglob("*") if p.is_file())


def _gain(root: Path, plan: dict):
    sources = sum(_safe_data_path(root, e["path"]).stat().st_blocks * 512 for e in plan["files"])
    # Conservatively charge the entire retained lake and archive/evidence folder,
    # including previously existing archives; no optimistic compression estimate.
    overhead = _allocated(root / "data-lake") + _allocated(Path(plan["archive"]).parent)
    fraction = (sources - overhead) / sources if sources else 0
    if fraction < plan["minimum_net_gain_fraction"]:
        raise DataLakeError(f"net allocated gain {fraction:.4f} is below 80%")
    return {
        "source_allocated_bytes": sources,
        "retained_overhead_bytes": overhead,
        "net_allocated_gain_bytes": sources - overhead,
        "net_gain_fraction": fraction,
    }


def _rehearse_h1_restore(archive: Path, plan: dict, manifest: dict) -> None:
    with tempfile.TemporaryDirectory(prefix="h1-restore-", dir=archive.parent) as temp:
        destination = Path(temp)
        with (
            archive.open("rb") as raw,
            zstd.ZstdDecompressor().stream_reader(raw) as stream,
            tarfile.open(fileobj=stream, mode="r|") as tar,
        ):
            tar.extractall(destination, filter="data")
        for e in plan["files"]:
            path = destination / "data" / e["path"]
            if path.stat().st_size != e["logical_bytes"] or sha256_file(path) != e["sha256"]:
                raise DataLakeError("H1 restore rehearsal mismatch")
        if json.loads((destination / "manifest.json").read_text(encoding="utf-8")) != manifest:
            raise DataLakeError("restored manifest mismatch")


def _archive_only_h1(root: Path, plan: dict, archive: Path, receipt: Path) -> dict:
    if receipt.exists():
        raise DataLakeError("verification receipt exists")
    manifest = _publish(root, plan, archive, validate_sources=_validate)
    _rehearse_h1_restore(archive, plan, manifest)
    _validate(plan, root)
    result = {
        "status": "verified",
        "archive_sha256": sha256_file(archive),
        "manifest": manifest,
        "restore_verified_files": len(plan["files"]),
        "storage": _gain(root, plan),
    }
    _json_new(receipt, result)
    return {k: v for k, v in result.items() if k != "manifest"}


def _prepare_retry_journal(journal: Path, archive_sha: str, fingerprint: str) -> str:
    if not journal.is_file() or journal.is_symlink():
        raise DataLakeError("restored retry requires a regular previous apply journal")
    previous = json.loads(journal.read_text(encoding="utf-8"))
    if (
        previous.get("status") not in ("applied", "interrupted", "prepared")
        or previous.get("archive_sha256") != archive_sha
        or previous.get("source_fingerprint") != fingerprint
    ):
        raise DataLakeError("previous apply journal does not match restored plan")
    saved = journal.with_name(journal.name + ".attempt-" + uuid.uuid4().hex)
    os.link(journal, saved)
    journal.unlink()
    return str(saved)


def _unlink_h1_sources(root: Path, files: list[dict], journal: Path, result: dict) -> None:
    for e in files:
        path = _safe_data_path(root, e["path"])
        if sha256_file(path) != e["sha256"]:
            raise DataLakeError("H1 source changed during apply")
        path.unlink()
        result["deleted"].append(e["path"])
        _atomic_write_json(journal, result)
    _prune_empty_parents(root, files)


def _apply_h1_archive(
    root: Path,
    plan: dict,
    archive: Path,
    receipt: Path,
    journal: Path,
    *,
    retry_restored: bool,
) -> dict:
    record = json.loads(receipt.read_text(encoding="utf-8"))
    manifest = record["manifest"]
    if (
        record.get("status") != "verified"
        or record.get("restore_verified_files") != len(plan["files"])
        or any(manifest.get(k) != v for k, v in plan.items())
        or sha256_file(archive) != record["archive_sha256"]
    ):
        raise DataLakeError("H1 verified archive/restore receipt mismatch")
    verify_archive(archive, manifest)
    storage = _gain(root, plan)
    _validate(plan, root)
    result: dict = {
        "status": "prepared",
        "archive_sha256": record["archive_sha256"],
        "source_fingerprint": plan["source_fingerprint"],
        "storage": storage,
        "deleted": [],
    }
    if retry_restored:
        result["previous_attempt_journal"] = _prepare_retry_journal(
            journal, record["archive_sha256"], plan["source_fingerprint"]
        )
    _json_new(journal, result)
    try:
        _unlink_h1_sources(root, plan["files"], journal, result)
        result["verification"] = verify_sealed_h1(root)
        result["status"] = "applied"
    except Exception:
        result["status"] = "interrupted"
        raise
    finally:
        _atomic_write_json(journal, result)
    return result


def execute(
    root: Path,
    plan_path: Path,
    mode: str,
    *,
    archive: Path | None = None,
    evidence: Path | None = None,
    retry_restored: bool = False,
) -> dict:
    resolved_root = root.resolve()
    with (resolved_root / ".data-archive-candidates.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if mode == "dry-run":
            if archive is None or evidence is None:
                raise DataLakeError("planning requires archive and evidence")
            plan = build_plan(resolved_root, archive, evidence)
            _json_new(plan_path, plan)
            return {
                "status": "planned",
                "source_count": len(plan["files"]),
                "source_fingerprint": plan["source_fingerprint"],
            }
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        _validate(plan, resolved_root)
        archive_path = Path(plan["archive"])
        receipt = archive_path.with_name(archive_path.name + ".verified.json")
        journal = archive_path.with_name(archive_path.name + ".apply.json")
        if mode == "archive-only":
            return _archive_only_h1(resolved_root, plan, archive_path, receipt)
        if mode != "apply":
            raise DataLakeError("unknown H1 archive mode")
        return _apply_h1_archive(
            resolved_root,
            plan,
            archive_path,
            receipt,
            journal,
            retry_restored=retry_restored,
        )
