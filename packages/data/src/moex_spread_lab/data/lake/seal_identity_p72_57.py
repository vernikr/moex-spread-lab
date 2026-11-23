"""Validate the path and record identity before trusting a sealed H1 source."""

from collections.abc import Callable
from pathlib import Path
from typing import Any


def validate_seal_record_identity(
    relative: str, record: dict[str, Any], path_type: type[Path], error_type: type[Exception]
) -> None:
    p = path_type(relative)
    if (
        p.is_absolute()
        or ".." in p.parts
        or len(p.parts) != 4
        or p.parts[:2] != ("data", "w1")
        or p.parts[2] not in ("candles", "candles-screen")
        or record["legacy_path"] != relative
        or record["timeframe"] != "1h"
    ):
        raise error_type("unsafe H1 seal source identity")


def check_no_symlink_ancestors(root: Path, legacy_path: Path, error_type: type[Exception]) -> None:
    for p in (legacy_path, *legacy_path.parents):
        if p == root:
            break
        if p.is_symlink():
            raise error_type("symlink H1 source is not allowed")


def verify_sealed_hashes(
    legacy_path: Path,
    legacy_signature: tuple[int, int, int, int] | None,
    bars: Path,
    sources: Path,
    record: dict[str, Any],
    hash_file: Callable[[Path], str],
    error_type: type[Exception],
) -> None:
    if legacy_signature is not None and hash_file(legacy_path) != record["legacy_sha256"]:
        raise error_type("retained H1 JSON changed after sealing; refusing stale Parquet")
    if (
        hash_file(bars) != record["parquet_sha256"]
        or hash_file(sources) != record["sources_sha256"]
    ):
        raise error_type("sealed H1 table hash mismatch; refusing fallback")
