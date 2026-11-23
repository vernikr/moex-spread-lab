"""Typed payload view and retention hooks for ``data_archive_candidates`` (P7.2 #67).

The owning module keeps its plan/receipt dicts and its historical names; this leaf
only restates what strict mode needs: the candidate entry view used by the archive
writer, and the path-safety hooks that ``data_retention`` declares as its surface.
Payload shapes, validation order and runtime behaviour are unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TypedDict

from moex_spread_lab.core.json_types import JsonObject, as_str_dict

from . import data_retention as _data_retention

_SafeDataPath = Callable[[Path, str], Path]
_ValidateArchivePath = Callable[[Path, Path], Path]
_PruneEmptyParents = Callable[[Path, Sequence[Mapping[str, object]]], None]


class _CandidateEntry(TypedDict):
    """One verified candidate file as stored in a plan, receipt or manifest."""

    path: str
    logical_bytes: int
    sha256: str


__all__ = ["_CandidateEntry", "read_payload_object", "retention_hooks"]


def retention_hooks() -> tuple[_SafeDataPath, _ValidateArchivePath, _PruneEmptyParents]:
    """Return the owning module's path-safety hooks in their historical order."""
    return _data_retention.archive_candidate_hooks()


def read_payload_object(path: Path, label: str) -> JsonObject:
    """Read a plan or receipt as a JSON object; a missing file still raises ``OSError``."""
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _data_retention.DataRetentionError(f"cannot read {label}: {path}: {exc}") from exc
    return as_str_dict(decoded, label=label, error=_data_retention.DataRetentionError)
