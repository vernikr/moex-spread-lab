"""Pyarrow stub adapters and manifest narrowing for ``lake/parquet_store.py`` (P7.2 leaf #64).

The installed `pyarrow-stubs==19.4` leaves several callables partially unknown
(`from_pylist` metadata, `ParquetFile.read` columns, `read_table` filter machinery),
so strict mode rejects them in the owning file. Each adapter below re-states only
the exact call shape `ParquetBarStore` uses and performs no runtime work beyond the
original call; payloads, dtypes and filter semantics are unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

import pyarrow as pa
import pyarrow.parquet as pq

from .bar_types import DataLakeError


class TableCalls(Protocol):
    """`pyarrow.Table` members consumed by the store, typed over the incomplete stubs."""

    @classmethod
    def from_pylist(
        cls, mapping: Sequence[Mapping[str, Any]], schema: pa.Schema | None = ...
    ) -> pa.Table: ...

    def to_pylist(self) -> list[dict[str, Any]]: ...


_TABLE = cast("type[TableCalls]", pa.Table)


def facade_hook(module: object, name: str) -> Any:
    """Resolve a private facade hook from a module owned by another P7.2 leaf.

    Those modules do not list the helpers in `__all__`, so this leaf may not import
    them directly and may not edit their owners; the caller re-types the result and
    keeps the binding at import time, exactly like the historical direct import.
    """
    return getattr(module, name)


def table_from_rows(rows: Sequence[Mapping[str, Any]], schema: pa.Schema) -> pa.Table:
    """Build a table from object-dict rows under an explicit schema."""
    return cast(TableCalls, _TABLE).from_pylist(rows, schema=schema)


def table_rows(table: pa.Table) -> list[dict[str, Any]]:
    """Convert a table to the object-dict rows the store consumes."""
    return cast(TableCalls, table).to_pylist()


def read_table(path: Path, columns: Iterable[str] | None, filters: Any = None) -> pa.Table:
    """Read one Parquet file, optionally projecting columns or pushing row filters."""
    projected = list(columns) if columns is not None else None
    if filters is None:
        return cast(pa.Table, cast(Any, pq.ParquetFile(path)).read(columns=projected))
    # Stub 19.4 rejects valid string comparison filters.
    return cast(pa.Table, cast(Any, pq.read_table)(path, columns=projected, filters=filters))


def as_dict(value: object) -> dict[str, Any] | None:
    """Return a JSON object mapping, or None for any other payload shape."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def manifest_records(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Narrow the manifest `datasets` list to object records, ignoring other items."""
    entries: object = manifest["datasets"]
    if not isinstance(entries, list):
        raise DataLakeError("data-lake manifest datasets must be a list")
    return [
        record for item in cast("list[object]", entries) if (record := as_dict(item)) is not None
    ]
