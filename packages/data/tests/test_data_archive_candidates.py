import io
import json
import tarfile
from pathlib import Path

import pytest
import zstandard as zstd

from moex_spread_lab.core.w0 import canonical_bytes
from moex_spread_lab.data.retention.data_archive_candidates import (
    _fingerprint,
    execute,
    verify_archive,
)
from moex_spread_lab.data.retention.data_retention import DataRetentionError


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    files = {
        "analytics/report.json": {"schema_version": "analytical-baseline.v1"},
        "w1/candles-common/A_1m.json": {"timeframe": "1m"},
        "w1/candles/A_10m.json": {"timeframe": "10m"},
        "w1/candles/A_1h.json": {"timeframe": "1h"},
        "w1/candles-screen/A_1h.json": {"timeframe": "1h"},
        "analytics/research-settings.json": {},
        "analytics/data-inventory-old.json": {"schema_version": "data-inventory.v1"},
        "analytics/runs/a/summary.json": {"schema_version": "analytical-baseline.v1"},
        "benchmark/value.json": {},
    }
    for name, body in files.items():
        path = root / "data" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body))
    return root, tmp_path / "plan.json", tmp_path / "archive.tar.zst"


def plan(setup):
    root, path, archive = setup
    return execute(root, path, "dry-run", archive)


def test_roundtrip_and_protection(setup):
    root, path, archive = setup
    before = {
        str(p.relative_to(root / "data")): p.read_bytes() for p in (root / "data").rglob("*.json")
    }
    result = plan(setup)
    assert result["file_count"] == 3
    assert not archive.exists()
    assert _fingerprint(result["files"]) == result["source_fingerprint"]
    verified = execute(root, path, "archive-only")
    assert all((root / "data" / p).read_bytes() == body for p, body in before.items())
    verify_archive(archive, verified["manifest"])
    # Restore rehearsal without extracting over working data.
    payload = zstd.ZstdDecompressor().stream_reader(io.BytesIO(archive.read_bytes())).read()
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        assert len(tar.getmembers()) == 4
        for entry in result["files"]:
            member = tar.extractfile("data/" + entry["path"])
            assert member is not None
            assert member.read() == before[entry["path"]]
    applied = execute(root, path, "apply")
    assert len(applied["deleted"]) == 3
    for name, body in before.items():
        source = root / "data" / name
        assert not source.exists() if name in applied["deleted"] else source.read_bytes() == body
    assert json.loads(Path(str(archive) + ".apply.json").read_text())["status"] == "applied"


@pytest.mark.parametrize("fault", ["stale-archive", "stale-apply", "corrupt", "no-receipt"])
def test_failed_apply_does_not_delete_sources(setup, fault):
    root, path, archive = setup
    result = plan(setup)
    if fault in {"stale-apply", "corrupt"}:
        execute(root, path, "archive-only")
    if fault.startswith("stale"):
        (root / "data" / result["files"][-1]["path"]).write_text("{}")
    if fault == "corrupt":
        archive.write_bytes(archive.read_bytes()[:30])
    mode = "archive-only" if fault == "stale-archive" else "apply"
    with pytest.raises(FileNotFoundError if fault == "no-receipt" else DataRetentionError):
        execute(root, path, mode)
    assert all((root / "data" / entry["path"]).exists() for entry in result["files"])


@pytest.mark.parametrize(
    "target", ["../outside", "/absolute", "w1/candles/A_1h.json", "analytics/runs/a/summary.json"]
)
def test_forged_plan_refused(setup, target):
    root, path, archive = setup
    plan(setup)
    result = json.loads(path.read_text())
    result["files"][0]["path"] = target
    result["files"].sort(key=lambda e: e["path"])
    result["source_fingerprint"] = _fingerprint(result["files"])
    path.write_text(json.dumps(result))
    with pytest.raises(DataRetentionError):
        execute(root, path, "archive-only")
    assert not archive.exists()


def test_existing_destination_and_symlink(setup):
    root, path, archive = setup
    result = plan(setup)
    archive.write_bytes(b"preserve")
    with pytest.raises(DataRetentionError):
        execute(root, path, "archive-only")
    assert archive.read_bytes() == b"preserve"
    archive.unlink()
    source = root / "data" / result["files"][0]["path"]
    copy = root / "copy.json"
    copy.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(copy)
    with pytest.raises(DataRetentionError):
        execute(root, path, "archive-only")


@pytest.mark.parametrize("fault", ["missing", "duplicate", "extra", "hash", "manifest", "symlink"])
def test_member_verification(setup, fault):
    root, path, archive = setup
    plan(setup)
    manifest = execute(root, path, "archive-only")["manifest"]
    archive.write_bytes(_faulty_archive(root, manifest, fault))
    with pytest.raises(DataRetentionError):
        verify_archive(archive, manifest)


def _add_faulty_member(tar, root, entry, fault):
    if fault == "missing":
        return
    body = (root / "data" / entry["path"]).read_bytes()
    if fault == "hash":
        body = b"x" * len(body)
    info = tarfile.TarInfo("data/" + entry["path"])
    info.size = len(body)
    if fault == "symlink":
        info.type = tarfile.SYMTYPE
        info.linkname = "elsewhere"
        info.size = 0
    tar.addfile(info, io.BytesIO(body))
    if fault == "duplicate":
        tar.addfile(info, io.BytesIO(body))


def _faulty_archive(root, manifest, fault):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        body = canonical_bytes(manifest) if fault != "manifest" else b"{}"
        info = tarfile.TarInfo("manifest.json")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
        for i, entry in enumerate(manifest["files"]):
            _add_faulty_member(tar, root, entry, fault if i == 0 else "")
        if fault == "extra":
            tar.addfile(tarfile.TarInfo("extra"))
    return zstd.ZstdCompressor().compress(buf.getvalue())


@pytest.mark.parametrize("fault", ["publication-race", "source-changed"])
def test_publication_races_fail_closed(setup, monkeypatch, fault):
    import moex_spread_lab.data.retention.data_archive_candidates as module

    root, path, archive = setup
    result = plan(setup)
    original = module.verify_archive

    def race(staging, manifest):
        original(staging, manifest)
        if fault == "publication-race":
            archive.write_bytes(b"other publisher")
        else:
            (root / "data" / result["files"][-1]["path"]).write_text("{}")

    monkeypatch.setattr(module, "verify_archive", race)
    with pytest.raises(FileExistsError if fault == "publication-race" else DataRetentionError):
        execute(root, path, "archive-only")
    if fault == "publication-race":
        assert archive.read_bytes() == b"other publisher"
    else:
        assert not archive.exists()
    assert all((root / "data" / entry["path"]).exists() for entry in result["files"])
    assert not list(archive.parent.glob(".archive.tar.zst.*"))
