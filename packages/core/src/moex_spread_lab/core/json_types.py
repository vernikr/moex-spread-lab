"""Re-export stub; canonical JSON-boundary helpers live in ``core.w0`` (#207)."""

from .w0 import (
    JSONBoundaryError,
    JsonObject,
    JsonScalar,
    JsonValue,
    as_str_dict,
    load_object,
)

__all__ = [
    "JSONBoundaryError",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "as_str_dict",
    "load_object",
]
