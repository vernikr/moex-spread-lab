"""Strict-typed helper primitives for ``data_refresh.py`` (P7.2 leaf #59)."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from .refresh_merge import DataRefreshError, RefreshTarget


class TargetStateProtocol(Protocol):
    @property
    def target(self) -> RefreshTarget: ...

    @property
    def legacy_path(self) -> Path: ...

    @property
    def payload(self) -> dict[str, Any] | None: ...

    @property
    def watermark(self) -> date | None: ...

    @property
    def requested_from(self) -> date: ...


class PayloadBuilder(Protocol):
    def __call__(
        self,
        *,
        rows: list[dict[str, Any]],
        page_count: int,
        chunk_count: int,
        raw_entry_count: int,
    ) -> dict[str, Any]: ...


def parse_iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DataRefreshError(f"{label} must be an ISO date: {value!r}") from exc


def parse_timestamp_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


@contextmanager
def dataset_writer_lock(lake_root: Path, target: RefreshTarget) -> Generator[None]:
    lock_name = hashlib.sha256(target.id.encode("utf-8")).hexdigest() + ".lock"
    lock_path = lake_root / "locks" / lock_name
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def date_chunks(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    if chunk_days < 1:
        raise DataRefreshError("chunk_days must be positive")
    result: list[tuple[date, date]] = []
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=chunk_days - 1), end)
        result.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return result


def parse_candle_row(row: object) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(row, dict):
        return None
    typed_row = cast(dict[str, Any], row)
    begin = typed_row.get("begin")
    if not isinstance(begin, str):
        return None
    return begin, dict(typed_row)
