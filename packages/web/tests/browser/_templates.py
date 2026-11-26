"""Cache fixture builders per worker; each scenario receives a private writable clone."""

from __future__ import annotations

import atexit
import hashlib
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_CACHE: dict[str, Path] = {}
_IMMUTABLE_VALUES = (str, bytes, int, float, complex, bool, type(None), frozenset)


def _fingerprint(value: Any) -> str:
    if isinstance(value, _IMMUTABLE_VALUES):
        return f"{type(value).__name__}:{value!r}"
    if isinstance(value, tuple):
        return "tuple:" + ",".join(_fingerprint(item) for item in value)
    if isinstance(value, frozenset | set):
        return f"{type(value).__name__}:" + ",".join(sorted(_fingerprint(item) for item in value))
    # A mutable capture may change during a build; its process-local identity does not.
    return f"{type(value).__name__}@{id(value)}"


def identity(builder: Callable[[Path], Any], key: str | None = None) -> str:
    """Closures sharing a qualified name can still build different fixtures."""
    if key:
        return key
    module = getattr(builder, "__module__", "") or ""
    name = getattr(builder, "__qualname__", None) or type(builder).__qualname__
    digest = hashlib.sha256()
    code = getattr(builder, "__code__", None)
    if code is not None:
        digest.update(code.co_code)
        digest.update(repr(code.co_consts).encode())
    for cell in getattr(builder, "__closure__", None) or ():
        try:
            digest.update(_fingerprint(cell.cell_contents).encode())
        except ValueError:  # an unbound cell
            digest.update(b"<empty>")
    digest.update(_fingerprint(getattr(builder, "__defaults__", None)).encode())
    return f"{module}.{name}#{digest.hexdigest()[:12]}"


def template_for(builder: Callable[[Path], Any], key: str | None = None) -> tuple[Path, float]:
    name = identity(builder, key)
    cached = _CACHE.get(name)
    if cached is not None:
        return cached, 0.0
    path = Path(tempfile.mkdtemp(prefix="moex-smoke-template-"))
    atexit.register(shutil.rmtree, path, True)
    started = time.perf_counter()
    builder(path)
    built = time.perf_counter() - started
    _CACHE[name] = path
    return path, built


def materialize(
    root: Path, builder: Callable[[Path], Any], key: str | None = None
) -> dict[str, float]:
    template, built = template_for(builder, key)
    clone_started = time.perf_counter()
    shutil.copytree(template, root, dirs_exist_ok=True)
    return {"template_build": built, "fixture_clone": time.perf_counter() - clone_started}


def cached() -> tuple[str, ...]:
    return tuple(sorted(_CACHE))
