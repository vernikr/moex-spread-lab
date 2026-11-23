"""Typed helpers for untrusted JSON boundaries in core.

JSON decoding returns ``Any``. Convert that value at the edge of the system so
validation code can carry a recursive JSON type instead of leaking ``Any``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
type JsonObject = dict[str, JsonValue]


class JSONBoundaryError(ValueError):
    """Raised when decoded input is not a JSON object with string keys."""


def as_str_dict(
    value: object,
    *,
    label: str = "JSON value",
    error: type[Exception] = JSONBoundaryError,
) -> JsonObject:
    """Validate and narrow a decoded object without allowing non-string keys."""
    if not isinstance(value, dict):
        raise error(f"{label} must be a JSON object with string keys")
    object_value = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in object_value):
        raise error(f"{label} must be a JSON object with string keys")
    return cast(JsonObject, value)


def load_object(
    path: str | Path,
    *,
    label: str | None = None,
    error: type[Exception] = JSONBoundaryError,
) -> JsonObject:
    """Read UTF-8 JSON from ``path`` and require an object root.

    Callers may provide their domain-specific exception class, keeping the
    boundary typed without changing public error contracts.
    """
    source = Path(path)
    context = label or str(source)
    try:
        decoded: object = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise error(f"cannot read {context} as UTF-8 JSON: {exc}") from exc
    return as_str_dict(decoded, label=context, error=error)
