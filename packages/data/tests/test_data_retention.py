import json
import tarfile

import pytest

from moex_spread_lab.data.retention import data_retention as retention
from moex_spread_lab.data.retention.data_inventory import build_data_inventory
from moex_spread_lab.data.retention.data_retention import DataRetentionError


@pytest.fixture
def cache_case(tmp_path):
    data = tmp_path / "data"
    paths = [data / "w1/candles-cache/X/60/page.json", data / "w1/candleborders-cache/X.json"]
    active = data / "w1/candles/X_1h.json"
    for path in [*paths, active]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"rows": []}')
    manifest = tmp_path / "inventory.json"
    manifest.write_text(json.dumps(build_data_inventory(tmp_path, include_hashes=True)))
    return tmp_path, paths, active, manifest, tmp_path / "archive.tar.gz"


def test_plan_and_archive(cache_case):
    root, sources, active, manifest, archive = cache_case
    plan = retention.plan_rebuildable_cache_archive(root, manifest, archive)
    assert plan["schema_version"] == retention.DATA_ARCHIVE_SCHEMA
    assert plan["status"] == "planned" and plan["file_count"] == 2 and plan["requires_apply"]
    result = retention.archive_rebuildable_caches(root, manifest, archive)
    assert result["status"] == "archived" and result["deleted_file_count"] == 2
    assert not any(path.exists() for path in sources) and active.exists()
    with tarfile.open(archive, "r:gz") as handle:
        names = set(handle.getnames())
        assert names == {
            "archive-manifest.json",
            *(f"data/{p.relative_to(root / 'data')}" for p in sources),
        }
        embedded = handle.extractfile("archive-manifest.json")
        assert embedded is not None
        assert len(json.load(embedded)["files"]) == 2


@pytest.mark.parametrize("fault", ["symlink", "stale", "forged", "data-root"])
def test_refuses_unsafe_sources_without_deletion(cache_case, fault):
    root, sources, active, manifest, archive = cache_case
    if fault == "symlink":
        sources[0].unlink()
        sources[0].symlink_to(active)
    elif fault == "stale":
        sources[0].write_text("changed")
    elif fault == "forged":
        value = json.loads(manifest.read_text())
        entry = next(item for item in value["files"] if item["path"].endswith("X_1h.json"))
        entry["classification"] = "rebuildable_cache"
        manifest.write_text(json.dumps(value))
    else:
        (root / "data").rename(root / "moved")
        (root / "data").symlink_to(root / "moved", target_is_directory=True)
    with pytest.raises(DataRetentionError):
        retention.archive_rebuildable_caches(root, manifest, archive)
    assert not archive.exists()
    assert all(path.exists() for path in sources) and active.exists()


@pytest.mark.parametrize("fault", ["publication", "source"])
def test_publication_races_fail_closed(cache_case, monkeypatch, fault):
    root, sources, active, manifest, archive = cache_case
    verify = retention.verify_tar

    def race(*args):
        verify(*args)
        if fault == "publication":
            archive.write_bytes(b"other writer")
        else:
            sources[-1].write_text("changed")

    monkeypatch.setattr(retention, "verify_tar", race)
    with pytest.raises(FileExistsError if fault == "publication" else DataRetentionError):
        retention.archive_rebuildable_caches(root, manifest, archive)
    if fault == "publication":
        assert archive.read_bytes() == b"other writer"
    else:
        assert not archive.exists()
    assert all(path.exists() for path in sources) and active.exists()
    assert not list(root.glob(".archive.tar.gz.*"))
