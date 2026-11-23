"""P7.2 export-compatibility regression for the lake and retention modules (issue #136).

Runtime ``import *`` measurement against the *true* pre-leaf revisions
(``5e97a62^`` for the lake module, ``9e53597^`` for the retention module) found
one supported-contract regression: #66 declared ``__all__`` in
``data/lake/refresh_merge.py`` and thereby dropped the imported names and
``DataRefreshError`` that star imports had always received. #67 then trimmed the
declaration further. ``data/retention/inventory_policy.py`` already shipped
``__all__`` before its leaf, so the #135 lead listing ``Any``/``Path``/``json``/
``dataclass``/``annotations`` for it describes incidental imports, not a
contract.
"""

from __future__ import annotations

import inspect
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from moex_spread_lab.data.lake import data_refresh, refresh_merge
from moex_spread_lab.data.retention import data_inventory, inventory_policy

LAKE = "moex_spread_lab.data.lake.refresh_merge"
RETENTION = "moex_spread_lab.data.retention.inventory_policy"

# Measured at 5e97a62^, the immediate pre-leaf revision of #66.
LAKE_STAR = {
    "Any",
    "CANDLE_INTERVAL_LABELS",
    "Callable",
    "DataLakeError",
    "DataRefreshError",
    "Iterator",
    "ParquetBarStore",
    "Path",
    "REFRESH_SCHEMA",
    "RefreshConfig",
    "RefreshTarget",
    "UTC",
    "annotations",
    "build_merged_payload",
    "contextmanager",
    "dataclass",
    "date",
    "datetime",
    "fcntl",
    "hashlib",
    "json",
    "os",
    "plan_single_target",
    "sha256_file",
    "tempfile",
    "timedelta",
}
# Private callables #66 published as module surface and #67 trimmed back out.
LAKE_HOOKS = (
    "_TargetState",
    "_atomic_write_json",
    "_chunks",
    "_dataset_writer_lock",
    "_date",
    "_timestamp_date",
)
LAKE_DECLARED = LAKE_STAR | set(LAKE_HOOKS)

# Measured at 9e53597^: the pre-leaf module already declared exactly these names.
RETENTION_STAR = {"DATA_INVENTORY_SCHEMA", "_Policy", "_inspect_file", "_path_timeframe", "_policy"}
RETENTION_INCIDENTAL = ("Any", "Path", "annotations", "dataclass", "json")


def _star_names(module_name: str) -> set[str]:
    namespace: dict[str, Any] = {}
    exec(f"from {module_name} import *", namespace)
    return set(namespace) - {"__builtins__"}


def test_lake_wildcard_surface_restores_the_pre_leaf_set() -> None:
    assert _star_names(LAKE) == LAKE_DECLARED
    assert set(refresh_merge.__all__) == LAKE_DECLARED
    assert set(refresh_merge.__all__) > LAKE_STAR
    for name in LAKE_HOOKS:
        assert callable(getattr(refresh_merge, name))
    assert isinstance(refresh_merge._TargetState, type)
    # ``cast``/``Generator`` arrived with the strict pass and were never surface.
    assert refresh_merge.cast is not None
    assert "cast" not in LAKE_DECLARED
    assert "Generator" not in LAKE_DECLARED
    assert refresh_merge.Iterator.__name__ == "Iterator"


def test_retention_wildcard_surface_is_the_pre_leaf_set() -> None:
    assert _star_names(RETENTION) == RETENTION_STAR
    assert set(inventory_policy.__all__) == RETENTION_STAR
    for incidental in RETENTION_INCIDENTAL:
        # Reachable by module attribute, never star-exported, exactly as before
        # the leaf commit.
        assert incidental not in RETENTION_STAR
        assert hasattr(inventory_policy, incidental)


def test_facade_identities_and_signatures_are_unchanged() -> None:
    for name in (*RETENTION_STAR - {"DATA_INVENTORY_SCHEMA"},):
        assert getattr(data_inventory, name) is getattr(inventory_policy, name)
    for name in (
        "REFRESH_SCHEMA",
        "DataRefreshError",
        "RefreshConfig",
        "RefreshTarget",
        "build_merged_payload",
        "plan_single_target",
    ):
        assert getattr(data_refresh, name) is getattr(refresh_merge, name)
    signatures = {
        "plan_single_target": (
            "(target: 'RefreshTarget', store: 'ParquetBarStore', cfg: 'RefreshConfig') "
            "-> 'tuple[_TargetState | None, dict[str, Any] | None]'"
        ),
        "_chunks": "(start: 'date', end: 'date', chunk_days: 'int') -> 'list[tuple[date, date]]'",
        "_atomic_write_json": "(path: 'Path', payload: 'dict[str, Any]') -> 'None'",
        "_dataset_writer_lock": (
            "(lake_root: 'Path', target: 'RefreshTarget') -> 'Generator[None, None, None]'"
        ),
    }
    for name, expected in signatures.items():
        assert str(inspect.signature(getattr(refresh_merge, name))) == expected


def test_lake_helpers_are_live_callables(tmp_path: Path) -> None:
    assert refresh_merge._chunks(date(2026, 1, 1), date(2026, 1, 3), 2) == [
        (date(2026, 1, 1), date(2026, 1, 2)),
        (date(2026, 1, 3), date(2026, 1, 3)),
    ]
    with pytest.raises(refresh_merge.DataRefreshError, match="chunk_days must be positive"):
        refresh_merge._chunks(date(2026, 1, 1), date(2026, 1, 2), 0)
    assert refresh_merge._date("2026-01-05", "probe") == date(2026, 1, 5)
    assert refresh_merge._timestamp_date("2026-01-05 10:00:00") == date(2026, 1, 5)
    assert refresh_merge._timestamp_date(None) is None
    written = tmp_path / "nested" / "payload.json"
    refresh_merge._atomic_write_json(written, {"b": 2, "a": 1})
    assert json.loads(written.read_text(encoding="utf-8")) == {"a": 1, "b": 2}
    assert not list(written.parent.glob(".payload.json.*.tmp"))
    # Explicit aliases preserve the objects while private hooks stay patchable.
    assert refresh_merge.dataset_writer_lock is refresh_merge._dataset_writer_lock
    assert refresh_merge.atomic_write_json is refresh_merge._atomic_write_json
    assert refresh_merge.chunks is refresh_merge._chunks


def test_dataset_writer_lock_is_exclusive_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations: list[int] = []
    real_flock = refresh_merge.fcntl.flock

    def tracking_flock(fd: int, operation: int) -> None:
        operations.append(operation)
        real_flock(fd, operation)

    monkeypatch.setattr(refresh_merge.fcntl, "flock", tracking_flock)
    with refresh_merge._dataset_writer_lock(tmp_path, refresh_merge.RefreshTarget("CNYRUBF", 24)):
        assert len(list((tmp_path / "locks").glob("*.lock"))) == 1
    assert operations == [refresh_merge.fcntl.LOCK_EX, refresh_merge.fcntl.LOCK_UN]


def test_data_refresh_keeps_its_own_helper_bindings() -> None:
    leaf_globals = data_refresh._atomic_write_json.__globals__
    for name, leaf_name in (
        ("_atomic_write_json", "atomic_write_json"),
        ("_chunks", "date_chunks"),
        ("_date", "parse_iso_date"),
        ("_timestamp_date", "parse_timestamp_date"),
        ("_dataset_writer_lock", "dataset_writer_lock"),
    ):
        assert getattr(data_refresh, name) is leaf_globals[leaf_name]
        assert getattr(data_refresh, name) is not getattr(refresh_merge, name)


def test_retention_hooks_are_dispatched_for_inventory_scans(tmp_path: Path) -> None:
    assert inventory_policy._path_timeframe("w1/candles/CNYRUBF_10m.json", {}) == "10m"
    assert inventory_policy._policy("benchmark-cache/spread.json", {}).dataset == "benchmark-cache"
    path = tmp_path / "candles.json"
    path.write_text(
        json.dumps(
            {"schema_version": "iss-candles.v1", "timeframe": "1h", "rows": [{"begin": "x"}]}
        ),
        encoding="utf-8",
    )
    assert inventory_policy._inspect_file(path)["time_range"] == {"from": "x", "till": "x"}
