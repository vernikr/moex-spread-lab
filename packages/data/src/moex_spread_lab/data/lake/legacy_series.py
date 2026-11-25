"""Legacy `iss-candles.v1` JSON parser, validator, and compatibility reader.

P7.2 (#62): JSON boundaries are narrowed with `cast`s placed after the existing
runtime checks; payloads, error text and the `__all__` wildcard surface are
unchanged.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.core.w0 import canonical_bytes

from .bar_types import LEGACY_CANDLE_SCHEMA, BarSeries, DataLakeError

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def _validate_proxy(
    row: dict[str, Any], close: float | None, path: Path, index: int, error_type: type[Exception]
) -> None:
    price_proxy = row.get("price_proxy")
    if price_proxy is None:
        return
    if isinstance(price_proxy, bool) or not isinstance(price_proxy, (int, float, str)):
        raise error_type(f"legacy candle price_proxy is invalid: {path}[{index}]")
    try:
        proxy_value = float(price_proxy)
    except ValueError as exc:
        raise error_type(f"legacy candle price_proxy is not numeric: {path}[{index}]") from exc
    if proxy_value != close:
        raise error_type(f"legacy candle price_proxy differs from close: {path}[{index}]")


# Freeze the original star-import surface; named aliases below support the facade.
__all__ = [
    "LEGACY_CANDLE_SCHEMA",
    "Any",
    "BarSeries",
    "DataLakeError",
    "Iterable",
    "LegacyCandleSeries",
    "LegacyJsonBarStore",
    "Path",
    "annotations",
    "canonical_bytes",
    "dataclass",
    "hashlib",
    "itertools",
    "json",
    "re",
]


@dataclass(frozen=True)
class LegacyCandleSeries:
    path: Path
    sha256: str
    secid: str
    board: str | None
    interval: int
    timeframe: str
    from_date: str | None
    till_date: str | None
    legacy_metadata: dict[str, Any]
    row_semantics: dict[str, Any]
    bars: list[dict[str, Any]]
    sources: list[dict[str, Any]]

    @property
    def dataset_id(self) -> str:
        return f"{self.timeframe}/{self.secid}"


def _component(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_COMPONENT.fullmatch(value):
        raise DataLakeError(f"invalid {label} for data-lake path: {value!r}")
    return value


def _number(value: Any, label: str, *, required: bool = False) -> float | None:
    if value is None:
        if required:
            raise DataLakeError(f"legacy candle {label} is missing")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataLakeError(f"legacy candle {label} must be numeric or null")
    return float(value)


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DataLakeError(f"legacy candle {label} must be a non-empty string")
    return value


def _integer(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataLakeError(f"legacy candle {label} must be an integer or null")
    integer = int(value)
    if integer != value:
        raise DataLakeError(f"legacy candle {label} must be an integer or null")
    return integer


def _source_key(source: dict[str, Any]) -> bytes:
    return canonical_bytes(
        {
            "url": source.get("url"),
            "received_at": source.get("received_at"),
            "raw_response_sha256": source.get("raw_response_sha256"),
        }
    )


def _load_legacy_payload(target: Path, display_path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = target.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataLakeError(f"cannot read legacy candle JSON: {display_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DataLakeError(f"legacy file is not {LEGACY_CANDLE_SCHEMA}: {display_path}")
    document = cast("dict[str, Any]", payload)
    if document.get("schema_version") != LEGACY_CANDLE_SCHEMA:
        raise DataLakeError(f"legacy file is not {LEGACY_CANDLE_SCHEMA}: {display_path}")
    return raw, document


def _validate_semantics(
    row: dict[str, Any], semantic_values: dict[str, Any], unset: object, path: Path
) -> None:
    for key in ("price_proxy_kind", "price_proxy_assumption"):
        value = row.get(key)
        if value is not None and not isinstance(value, str):
            raise DataLakeError(f"legacy candle {key} must be a string or null")
        previous = semantic_values[key]
        if previous is unset:
            semantic_values[key] = value
        elif value != previous:
            raise DataLakeError(f"legacy candle {key} is not constant: {path}")


def _validate_price_proxy(row: dict[str, Any], close: float | None, path: Path, index: int) -> None:
    _validate_proxy(row, close, path, index, DataLakeError)


def _record_row_source(
    source: Any,
    begin: str,
    sources: list[dict[str, Any]],
    source_ids: dict[bytes, int],
    path: Path,
    index: int,
) -> int | None:
    if source is None:
        return None
    if not isinstance(source, dict):
        raise DataLakeError(f"legacy candle source is invalid: {path}[{index}]")
    fields = cast("dict[str, Any]", source)
    page_provenance: dict[str, str | None] = {}
    for field in ("url", "received_at", "raw_response_sha256"):
        value = fields.get(field)
        if value is not None and not isinstance(value, str):
            raise DataLakeError(
                f"legacy candle source {field} must be a string or null: {path}[{index}]"
            )
        page_provenance[field] = value
    raw_hash = page_provenance["raw_response_sha256"]
    if raw_hash is not None and not _HEX64.fullmatch(raw_hash):
        raise DataLakeError(f"legacy candle source raw_response_sha256 is invalid: {path}[{index}]")
    key = _source_key(page_provenance)
    source_page_id = source_ids.get(key)
    if source_page_id is None:
        source_page_id = len(sources)
        source_ids[key] = source_page_id
        sources.append(
            {
                "source_page_id": source_page_id,
                **page_provenance,
                "first_begin": begin,
                "last_begin": begin,
                "row_count": 0,
            }
        )
    page = sources[source_page_id]
    page["last_begin"] = begin
    page["row_count"] += 1
    return source_page_id


def _parse_legacy_row(
    row: Any,
    index: int,
    path: Path,
    semantic_values: dict[str, Any],
    unset: object,
    sources: list[dict[str, Any]],
    source_ids: dict[bytes, int],
) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise DataLakeError(f"legacy candle row is not an object: {path}[{index}]")
    values = cast("dict[str, Any]", row)
    begin = _timestamp(values.get("begin"), "begin")
    end = _timestamp(values.get("end"), "end")
    close = _number(values.get("close"), "close", required=True)
    _validate_semantics(values, semantic_values, unset, path)
    _validate_price_proxy(values, close, path, index)
    source_page_id = _record_row_source(
        values.get("source"), begin, sources, source_ids, path, index
    )
    return {
        "begin": begin,
        "end": end,
        "open": _number(values.get("open"), "open"),
        "high": _number(values.get("high"), "high"),
        "low": _number(values.get("low"), "low"),
        "close": close,
        "value": _number(values.get("value"), "value"),
        "volume": _integer(values.get("volume"), "volume"),
        "source_page_id": source_page_id,
    }


def _validate_legacy_header(
    payload: dict[str, Any], path: Path
) -> tuple[str, str, int, str | None, list[Any]]:
    secid = _component(payload.get("secid"), "SECID")
    timeframe = _component(payload.get("timeframe"), "timeframe")
    interval = payload.get("interval")
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        raise DataLakeError(f"legacy candle interval is invalid: {path}")
    board = payload.get("board")
    if board is not None and not isinstance(board, str):
        raise DataLakeError(f"legacy candle board is invalid: {path}")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise DataLakeError(f"legacy candle series has no rows: {path}")
    return secid, timeframe, interval, board, cast("list[Any]", rows)


def _read_legacy_series(path: Path) -> LegacyCandleSeries:
    resolved = path.resolve()
    raw, payload = _load_legacy_payload(resolved, path)
    secid, timeframe, interval, board, rows = _validate_legacy_header(payload, path)
    unset = object()
    semantic_values: dict[str, Any] = {"price_proxy_kind": unset, "price_proxy_assumption": unset}
    sources: list[dict[str, Any]] = []
    source_ids: dict[bytes, int] = {}
    bars = [
        _parse_legacy_row(row, idx, path, semantic_values, unset, sources, source_ids)
        for idx, row in enumerate(rows)
    ]
    if any(left["begin"] >= right["begin"] for left, right in itertools.pairwise(bars)):
        raise DataLakeError(f"legacy candle timestamps are not strictly increasing: {path}")
    return LegacyCandleSeries(
        path=resolved,
        sha256=hashlib.sha256(raw).hexdigest(),
        secid=secid,
        board=board,
        interval=interval,
        timeframe=timeframe,
        from_date=payload.get("from") if isinstance(payload.get("from"), str) else None,
        till_date=payload.get("till") if isinstance(payload.get("till"), str) else None,
        legacy_metadata={key: value for key, value in payload.items() if key != "rows"},
        row_semantics={
            "price_proxy_kind": semantic_values["price_proxy_kind"],
            "price_proxy_assumption": semantic_values["price_proxy_assumption"],
            "price_proxy_matches_close": True,
        },
        bars=bars,
        sources=sources,
    )


def _project_legacy_row(row: dict[str, Any], columns: tuple[str, ...] | None) -> dict[str, Any]:
    if columns is None:
        return dict(row)
    projected = {key: row[key] for key in columns if key in row}
    if "price_proxy_kind" in row:
        projected["price_proxy_kind"] = row["price_proxy_kind"]
    return projected


class LegacyJsonBarStore:
    """Compatibility reader for existing ``iss-candles.v1`` files.

    It intentionally accepts the historical lightweight fixture shape too. The
    stricter ``_read_legacy_series`` remains the importer validation boundary.
    """

    def read(self, legacy_path: Path, *, columns: Iterable[str] | None = None) -> BarSeries:
        try:
            raw = legacy_path.read_bytes()
            payload = json.loads(raw)
            stat = legacy_path.stat()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataLakeError(f"cannot read legacy candle JSON: {legacy_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise DataLakeError(f"legacy file is not {LEGACY_CANDLE_SCHEMA}: {legacy_path}")
        document = cast("dict[str, Any]", payload)
        if document.get("schema_version") != LEGACY_CANDLE_SCHEMA:
            raise DataLakeError(f"legacy file is not {LEGACY_CANDLE_SCHEMA}: {legacy_path}")
        raw_rows = document.get("rows")
        if not isinstance(raw_rows, list):
            raise DataLakeError(f"legacy candle rows must be a list: {legacy_path}")
        body = cast("list[Any]", raw_rows)
        if any(not isinstance(row, dict) for row in body):
            raise DataLakeError(f"legacy candle row is not an object: {legacy_path}")
        rows = cast("list[dict[str, Any]]", body)
        interval = document.get("interval")
        if isinstance(interval, bool) or not isinstance(interval, int):
            interval = None
        secid = document.get("secid") if isinstance(document.get("secid"), str) else None
        timeframe = (
            document.get("timeframe") if isinstance(document.get("timeframe"), str) else None
        )
        col_tuple = tuple(columns) if columns is not None else None
        return BarSeries(
            schema_version=LEGACY_CANDLE_SCHEMA,
            secid=secid,
            interval=interval,
            timeframe=timeframe,
            rows=tuple(_project_legacy_row(row, col_tuple) for row in rows),
            source_path=str(legacy_path),
            source_sha256=hashlib.sha256(raw).hexdigest(),
            backend="legacy_json",
            cache_key=("legacy_json", str(legacy_path.resolve()), stat.st_mtime_ns, stat.st_size),
            metadata={key: value for key, value in document.items() if key != "rows"},
        )


component = _component
integer = _integer
number = _number
read_legacy_series = _read_legacy_series
source_key = _source_key
timestamp = _timestamp
