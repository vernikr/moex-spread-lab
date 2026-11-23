"""Strict-mode contract of ``data_archive_candidates`` (issue #67).

The P7.2 strict pass only adds annotations and payload views: the CLI surface,
the plan/receipt/report payloads, the path-safety and wildcard selection
contracts, and the archive verification guarantees must stay identical.
This module pins them so the conversion cannot drift.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any, cast, get_type_hints

import pytest
import zstandard as zstd

from moex_spread_lab.core.json_types import JSONBoundaryError
from moex_spread_lab.core.w0 import canonical_bytes
from moex_spread_lab.data.retention import data_archive_candidates, data_retention
from moex_spread_lab.data.retention.data_archive_candidates import (
    GROUPS,
    SCHEMA,
    _eligible,
    _fingerprint,
    _publish,
    build_plan,
    execute,
    verify_archive,
)
from moex_spread_lab.data.retention.data_retention import DataRetentionError

_SUFFIX = "p72_packages_data_src_moex_spread_lab_data_retention_data_archive_candidates"

_PLAN_KEYS = (
    "schema_version",
    "root",
    "archive",
    "files",
    "source_fingerprint",
    "file_count",
    "logical_bytes",
)

_ARCHIVE_KEYS = ("status", "archive_sha256", "manifest")

_APPLIED_KEYS = ("status", "archive", "archive_sha256", "source_fingerprint", "deleted")


def _as_object(value: object) -> dict[str, Any]:
    """Narrow a JSON payload member to an object for the assertions below."""
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Narrow a plan, receipt or manifest ``files`` array to object entries."""
    return [cast(dict[str, Any], item) for item in cast(list[object], payload["files"])]


def _git_repo(root: Path) -> Path:
    import subprocess

    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "test",
        ],
        cwd=root,
        check=True,
    )
    return root


@pytest.fixture
def setup(tmp_path):
    root = _git_repo(tmp_path / "repo")
    files = {
        "analytics/report.json": {"schema_version": "analytical-baseline.v1"},
        "w1/candles-common/A_1m.json": {"timeframe": "1m"},
        "w1/candles/A_10m.json": {"timeframe": "10m"},
        "w1/candles/A_1h.json": {"timeframe": "1h"},
        "analytics/research-settings.json": {},
    }
    for name, body in files.items():
        path = root / "data" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body))
    return root, tmp_path / "plan.json", tmp_path / "archive.tar.zst"


def test_facade_surface_stays_resolvable_and_typed():
    assert SCHEMA == "data-archive-candidates.v1"
    assert set(GROUPS) == {
        "analytics/derived",
        "w1/candles-common",
        "w1/candles/10m",
        "w1/candles/unknown",
        "w1/perpetual-history-discovery",
    }
    for name in ("execute", "build_plan", "verify_archive", "_publish", "_json_new", "_eligible"):
        assert callable(getattr(data_archive_candidates, name)), name
    assert callable(data_archive_candidates._safe_data_path)
    assert callable(data_archive_candidates._prune_empty_parents)
    assert callable(data_archive_candidates._validate_archive_path)
    hints = get_type_hints(execute)
    assert set(hints) == {"root", "plan_path", "mode", "archive", "return"}
    assert hints["archive"] == Path | None


def test_retention_hooks_are_declared_surface_and_identity_preserved():
    for hook in ("_prune_empty_parents", "_safe_data_path", "_validate_archive_path"):
        assert hook in data_retention.__all__
        assert getattr(data_archive_candidates, hook) is getattr(data_retention, hook)
    assert "DATA_ARCHIVE_SCHEMA" in data_retention.__all__


def test_plan_payload_keeps_its_json_shape(setup):
    root, path, archive = setup
    plan = execute(root, path, "dry-run", archive)
    files = _entries(plan)
    assert tuple(plan) == ("status", *_PLAN_KEYS)
    assert plan["schema_version"] == SCHEMA
    assert plan["file_count"] == 3
    assert plan["logical_bytes"] == sum(entry["logical_bytes"] for entry in files)
    assert [entry["path"] for entry in files] == [
        "analytics/report.json",
        "w1/candles-common/A_1m.json",
        "w1/candles/A_10m.json",
    ]
    assert all(tuple(entry) == ("path", "logical_bytes", "sha256") for entry in files)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == {
        key: value for key, value in json.loads(json.dumps(plan)).items() if key != "status"
    }
    assert _fingerprint(stored["files"]) == stored["source_fingerprint"]


def test_archive_and_apply_payloads_keep_their_json_shape(setup):
    root, path, archive = setup
    execute(root, path, "dry-run", archive)
    verified = execute(root, path, "archive-only")
    assert tuple(verified) == _ARCHIVE_KEYS
    assert verified["status"] == "verified"
    manifest = _as_object(verified["manifest"])
    assert manifest["git_commit"] and manifest["created_at"]
    assert manifest["source_fingerprint"] == json.loads(path.read_text())["source_fingerprint"]
    applied = execute(root, path, "apply")
    assert tuple(applied) == _APPLIED_KEYS
    assert applied["status"] == "applied"
    assert applied["deleted"] == [entry["path"] for entry in _entries(manifest)]
    report = json.loads(Path(str(archive) + ".apply.json").read_text(encoding="utf-8"))
    assert report == json.loads(json.dumps(applied))
    assert not (root / "data" / "w1" / "candles" / "A_10m.json").exists()
    assert not (root / "data" / "analytics" / "report.json").exists()
    assert not (root / "data" / "w1" / "candles-common" / "A_1m.json").exists()
    assert not (root / "data" / "w1" / "candles-common").exists()


def test_wildcard_and_analytics_selection_contract():
    candidate = {"extension": ".json", "schema_version": "iss-candles.v1"}
    assert _eligible("w1/candles-common/A_1m.json", candidate)
    assert _eligible("w1/candles/10m/A.json", {"timeframe": "10m"})
    assert not _eligible("w1/candles/A_1h.json", {"timeframe": "1h"})
    assert not _eligible("analytics/research-settings.json", {})
    assert not _eligible(
        "analytics/data-inventory-old.json", {"schema_version": "data-inventory.v1"}
    )
    assert not _eligible(
        "analytics/runs/a/summary.json", {"schema_version": "analytical-baseline.v1"}
    )
    assert not _eligible("benchmark/value.json", {})


@pytest.mark.parametrize("target", ["../outside", "/absolute", "w1/candles/A_1h.json"])
def test_path_safety_rejects_forged_plans(setup, target):
    root, path, archive = setup
    execute(root, path, "dry-run", archive)
    forged = json.loads(path.read_text(encoding="utf-8"))
    forged["files"][0]["path"] = target
    forged["files"].sort(key=lambda entry: entry["path"])
    forged["source_fingerprint"] = _fingerprint(forged["files"])
    path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(DataRetentionError):
        execute(root, path, "archive-only")
    assert not archive.exists()
    assert all((root / "data" / name).exists() for name in ("analytics/report.json",))


def test_archive_outside_data_is_enforced(setup):
    root, path, _archive = setup
    with pytest.raises(DataRetentionError):
        build_plan(root, root / "data" / "archive.tar.zst")
    with pytest.raises(DataRetentionError):
        execute(root, path, "dry-run", root / "data" / "archive.tar.zst")
    assert not path.exists()


def test_unreadable_payloads_raise_the_documented_error(setup):
    root, path, archive = setup
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(DataRetentionError):
        execute(root, path, "archive-only")
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(DataRetentionError):
        execute(root, path, "archive-only")
    path.unlink()
    execute(root, path, "dry-run", archive)
    execute(root, path, "archive-only")
    receipt = Path(str(archive) + ".verified.json")
    receipt.write_text("[]", encoding="utf-8")
    with pytest.raises(DataRetentionError):
        execute(root, path, "apply")


def test_publish_calls_validate_sources_before_publication(setup, tmp_path):
    root, path, archive = setup
    plan = json.loads(json.dumps(execute(root, path, "dry-run", archive)))
    calls: list[Path] = []

    def reject(candidate: dict[str, Any], candidate_root: Path) -> None:
        calls.append(candidate_root)
        raise DataRetentionError("rejected by caller")

    with pytest.raises(DataRetentionError, match="rejected by caller"):
        _publish(root, plan, tmp_path / "out.tar.zst", validate_sources=reject)
    assert calls == [root]
    assert not (tmp_path / "out.tar.zst").exists()
    assert not list((tmp_path).glob(".out.tar.zst.*"))


def test_embedded_manifest_must_match_the_published_bytes(setup):
    root, path, archive = setup
    execute(root, path, "dry-run", archive)
    manifest = _as_object(execute(root, path, "archive-only")["manifest"])
    tampered = {**manifest, "file_count": 99}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        body = canonical_bytes(tampered)
        info = tarfile.TarInfo("manifest.json")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
        for entry in _entries(manifest):
            payload = (root / "data" / entry["path"]).read_bytes()
            info = tarfile.TarInfo("data/" + entry["path"])
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    archive.write_bytes(zstd.ZstdCompressor().compress(buffer.getvalue()))
    with pytest.raises(DataRetentionError, match="embedded manifest mismatch"):
        verify_archive(archive, manifest)


def test_payload_boundary_errors_stay_domain_errors(setup):
    root, path, archive = setup
    execute(root, path, "dry-run", archive)
    receipt = Path(str(archive) + ".verified.json")
    receipt.write_text("", encoding="utf-8")
    with pytest.raises(DataRetentionError) as excinfo:
        execute(root, path, "apply")
    assert not isinstance(excinfo.value, JSONBoundaryError)
    assert "cannot read verification receipt" in str(excinfo.value)


def test_write_once_plans_and_receipts_are_never_overwritten(setup, tmp_path):
    root, path, archive = setup
    execute(root, path, "dry-run", archive)
    with pytest.raises(FileExistsError):
        execute(root, path, "dry-run", archive)
    execute(root, path, "archive-only")
    with pytest.raises(DataRetentionError, match="verification receipt already exists"):
        execute(root, path, "archive-only")
    other_plan = tmp_path / "other-plan.json"
    with pytest.raises(DataRetentionError, match="archive destination already exists"):
        execute(root, other_plan, "dry-run", archive)
