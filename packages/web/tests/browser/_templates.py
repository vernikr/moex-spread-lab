"""Session-scoped fixture templates cloned per browser scenario (plan S4b, work 3).

Every smoke suite builds its synthetic workspace with a builder such as
`proof_fixtures.fixture` or `proof_fixtures.two_calendar_windows`. Before S4b each
script paid that build again in its own process; now the first caller in a worker
process builds a template once and every later caller receives a byte-for-byte copy
inside its own temporary root. Suites that mutate their workspace (the funding smoke
rewrites funding rates, the strategy smoke shocks candle prices) therefore keep
touching only their clone, never the template or another scenario.

Identity is the builder's module-qualified name plus an optional explicit `key`, so
two builders with different module paths can never share a template.
"""

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
#: Values whose repr is stable enough to identify a closure; anything else (a list a
#: builder appends to, for instance) is identified by object identity, which is stable
#: for the life of the process and whose cache this is.
_IMMUTABLE_VALUES = (str, bytes, int, float, complex, bool, type(None), frozenset)


def _fingerprint(value: Any) -> str:
    if isinstance(value, _IMMUTABLE_VALUES):
        return f"{type(value).__name__}:{value!r}"
    if isinstance(value, tuple):
        return "tuple:" + ",".join(_fingerprint(item) for item in value)
    if isinstance(value, frozenset | set):
        return f"{type(value).__name__}:" + ",".join(sorted(_fingerprint(item) for item in value))
    return f"{type(value).__name__}@{id(value)}"


def identity(builder: Callable[[Path], Any], key: str | None = None) -> str:
    """Stable cache key for a fixture builder.

    Module and qualified name alone are not enough for closures and lambdas: two
    closures from one factory share both while writing different fixtures. Their code,
    defaults and captured values therefore join the key. A builder called with
    non-default keyword arguments must pass an explicit ``key``.
    """
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
    """Build (once per process) and return the template directory plus build seconds."""
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
    """Fill ``root`` with a private copy of the builder's template.

    Returns the measured ``template_build`` (``0.0`` on a cache hit) and
    ``fixture_clone`` seconds, so the harness attributes them to the phase budget
    instead of guessing.
    """
    template, built = template_for(builder, key)
    clone_started = time.perf_counter()
    shutil.copytree(template, root, dirs_exist_ok=True)
    return {"template_build": built, "fixture_clone": time.perf_counter() - clone_started}


def cached() -> tuple[str, ...]:
    """Builders with a warm template in this process (used by diagnostics and tests)."""
    return tuple(sorted(_CACHE))
