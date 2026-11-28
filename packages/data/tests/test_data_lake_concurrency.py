import json
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from moex_spread_lab.data.lake import raw_archive
from moex_spread_lab.data.lake.parquet_store import ParquetBarStore
from moex_spread_lab.data.lake.raw_archive import RawResponseArchive


def _legacy(root: Path, secid: str) -> Path:
    path = root / "data" / "w1" / "candles" / f"{secid}_1h.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "begin": "2026-01-01 10:00:00",
        "end": "2026-01-01 10:59:59",
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": 1.0,
        "value": 1.0,
        "volume": 1,
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": "iss-candles.v1",
                "secid": secid,
                "interval": 60,
                "timeframe": "1h",
                "rows": [row],
            }
        )
    )
    return path


def _import_many(root: Path, paths: list[Path], start) -> None:
    store = ParquetBarStore(root)
    start.wait()
    for path in paths:
        store.import_legacy(path)


def _archive_many(root: Path, prefix: str, start) -> None:
    archive = RawResponseArchive(root / "data-lake")
    start.wait()
    for index in range(20):
        archive.archive(
            url=f"https://example.test/{prefix}/{index}",
            raw=b"{}",
            received_at="2026-01-01T00:00:00+00:00",
            pin=True,
        )


def _run_pair(target, first, second):
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    processes = [ctx.Process(target=target, args=(*args, start)) for args in (first, second)]
    for process in processes:
        process.start()
    start.set()
    try:
        for process in processes:
            process.join(30)
        assert all(not process.is_alive() and process.exitcode == 0 for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()


def test_concurrent_imports_keep_all_manifest_records(tmp_path):
    groups = [[_legacy(tmp_path, f"{prefix}{i:02d}") for i in range(25)] for prefix in "AB"]
    _run_pair(_import_many, (tmp_path, groups[0]), (tmp_path, groups[1]))
    store = ParquetBarStore(tmp_path)
    assert len(store.read_manifest()["datasets"]) == 50
    assert store.verify_all()["dataset_count"] == 50


def test_upsert_recovers_unregistered_parquet(tmp_path):
    path = _legacy(tmp_path, "ABC")
    store = ParquetBarStore(tmp_path)
    store.import_legacy(path)
    store.manifest_path.unlink()
    store.upsert_legacy(path)
    assert store.verify_legacy(path)["status"] == "verified"


def test_concurrent_raw_archives_keep_all_entries(tmp_path):
    _run_pair(_archive_many, (tmp_path, "A"), (tmp_path, "B"))
    result = RawResponseArchive(tmp_path / "data-lake").verify()
    assert result["entry_count"] == 40
    assert result["orphan_files"] == []


def test_raw_read_operations_do_not_create_lake(tmp_path):
    archive = RawResponseArchive(tmp_path / "data-lake")
    assert archive.gc(dry_run=True)["status"] == "dry_run"
    assert archive.verify()["entry_count"] == 0
    assert not list(tmp_path.iterdir())


def test_raw_gc_cannot_delete_inflight_archive(tmp_path, monkeypatch):
    archive = RawResponseArchive(tmp_path / "data-lake")
    stored = threading.Event()
    release = threading.Event()
    original = raw_archive.atomic_write_bytes

    def pause_after_write(path, value):
        original(path, value)
        stored.set()
        assert release.wait(10)

    monkeypatch.setattr(raw_archive, "atomic_write_bytes", pause_after_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(
            archive.archive,
            url="https://example.test/a",
            raw=b"{}",
            received_at="2026-01-01T00:00:00+00:00",
            pin=True,
        )
        try:
            assert stored.wait(10)
            gc = executor.submit(archive.gc, dry_run=False)
            try:
                result = gc.result(timeout=0.5)
            except TimeoutError:
                pass
            else:
                assert result["planned_files"] == []
        finally:
            release.set()
        future.result(timeout=10)
        gc.result(timeout=10)
    assert archive.verify()["entry_count"] == 1
