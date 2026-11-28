"""Lazy loader and atomic clang builder for the optional C99 `_fastcore` extension."""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import math
import os
import shutil
import subprocess
import sys
import sysconfig
import threading
import warnings
from contextlib import suppress
from pathlib import Path
from typing import Any, NamedTuple, cast

_LOCK = threading.Lock()
_CACHE: dict[bool, Any] = {}


def _has_asan_runtime() -> bool:
    """Return True if the AddressSanitizer runtime is already loaded in this process."""
    try:
        proc = ctypes.CDLL(None)
        return hasattr(proc, "__asan_init")
    # CDLL/symbol probes fail with platform OSError/AttributeError on foreign builds.
    except (OSError, AttributeError):
        return False


def _build_compile_command(
    cc: str,
    inc: str,
    c_path: Path,
    tmp_so: Path,
    *,
    sanitize: bool,
) -> list[str]:
    is_darwin = os.uname().sysname == "Darwin"
    cmd = [cc, "-std=c99"]
    if sanitize:
        cmd.extend(
            [
                "-DMOEX_FASTCORE_SANITIZE=1",
                "-O1",
                "-g",
                "-fno-omit-frame-pointer",
                "-fsanitize=address,undefined",
                "-fno-sanitize-recover=all",
            ]
        )
        if not is_darwin:
            cmd.append("-shared-libasan")
    else:
        cmd.append("-O3")
    cmd.extend(
        [
            "-ffp-contract=off",
            "-shared",
            "-fPIC",
            f"-I{inc}",
            "-Wall",
            "-Wextra",
            "-Wno-unused-function",
            str(c_path),
            "-o",
            str(tmp_so),
            "-lm",
        ]
    )
    if is_darwin:
        cmd.extend(["-undefined", "dynamic_lookup"])
    return cmd


def _compiler_env() -> dict[str, str]:
    excluded = {
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "ASAN_OPTIONS",
        "LSAN_OPTIONS",
        "UBSAN_OPTIONS",
    }
    return {key: value for key, value in os.environ.items() if key not in excluded}


def _read_stamp(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_stamp(path: Path, value: str) -> None:
    # Best-effort freshness marker; a failed write only costs one rebuild.
    with suppress(OSError):
        path.write_text(value + "\n", encoding="utf-8")


def _build_fingerprint(c_bytes: bytes, *, sanitize: bool, cc: str) -> str:
    digest = hashlib.sha256()
    digest.update(c_bytes)
    digest.update(b"\x00")
    digest.update(b"sanitize" if sanitize else b"release")
    digest.update(b"\x00")
    digest.update(sys.version.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(cc.encode("utf-8"))
    return digest.hexdigest()


def _drop_stale_binary(so_path: Path, stamp_path: Path) -> None:
    """A kernel that does not match the source must not be loaded."""
    so_path.unlink(missing_ok=True)
    stamp_path.unlink(missing_ok=True)


def _ensure_compiled(pkg_dir: Path, ext: str, *, sanitize: bool) -> Path:
    stem = "_fastcore_asan" if sanitize else "_fastcore"
    c_path = pkg_dir / "_fastcore.c"
    so_path = pkg_dir / f"{stem}{ext}"
    stamp_path = so_path.with_name(so_path.name + ".stamp")
    fail_path = so_path.with_name(so_path.name + ".failed")
    if not c_path.is_file():
        return so_path
    try:
        c_bytes = c_path.read_bytes()
    except OSError:
        return so_path
    cc = shutil.which("clang") or (None if sanitize else shutil.which("cc"))
    cc = cc or ""
    inc = sysconfig.get_path("include")
    if not (cc and inc and Path(inc, "Python.h").is_file()):
        if sanitize:
            raise RuntimeError(
                "MOEX_FASTCORE_SANITIZE=1 requires clang and Python development headers"
            )
        return so_path
    desired = _build_fingerprint(c_bytes, sanitize=sanitize, cc=cc)
    if so_path.is_file() and _read_stamp(stamp_path) == desired:
        return so_path
    if _read_stamp(fail_path) == desired:
        # Known-bad source for this toolchain: do not rebuild on every worker start.
        _drop_stale_binary(so_path, stamp_path)
        return so_path
    return _build_and_install(
        _BuildRequest(pkg_dir, c_path, so_path, cc, inc, ext, sanitize, desired, stem)
    )


class _BuildRequest(NamedTuple):
    pkg_dir: Path
    c_path: Path
    so_path: Path
    cc: str
    inc: str
    ext: str
    sanitize: bool
    desired: str
    stem: str


def _build_and_install(request: _BuildRequest) -> Path:
    so_path = request.so_path
    stamp_path = so_path.with_name(so_path.name + ".stamp")
    fail_path = so_path.with_name(so_path.name + ".failed")
    tmp_so = request.pkg_dir / f"{request.stem}.tmp.{os.getpid()}{request.ext}"
    cmd = _build_compile_command(
        request.cc, request.inc, request.c_path, tmp_so, sanitize=request.sanitize
    )
    try:
        res = subprocess.run(cmd, env=_compiler_env(), check=False, capture_output=True, text=True)
        if res.returncode != 0:
            stderr_tail = (res.stderr or "").strip()[-500:]
            _drop_stale_binary(so_path, stamp_path)
            _write_stamp(fail_path, request.desired)
            if request.sanitize:
                raise RuntimeError(
                    f"Sanitized _fastcore build failed ({res.returncode}): {stderr_tail}"
                )
            warnings.warn(
                f"_fastcore rebuild failed; falling back to pure Python. Compiler stderr: {stderr_tail}",
                RuntimeWarning,
                stacklevel=2,
            )
            return so_path
        os.replace(tmp_so, so_path)
        _write_stamp(stamp_path, request.desired)
        fail_path.unlink(missing_ok=True)
    finally:
        tmp_so.unlink(missing_ok=True)
        shutil.rmtree(f"{tmp_so}.dSYM", ignore_errors=True)
    return so_path


def _import_shared_module(so_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("moex_spread_lab.core._fastcore", so_path)
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    return None


def _load_sanitized_fastcore(pkg_dir: Path, ext: str) -> Any:
    if not _has_asan_runtime():
        raise RuntimeError(
            "MOEX_FASTCORE_SANITIZE=1 requires preloading the clang AddressSanitizer runtime "
            "(run via `uv run python scripts/run_sanitized_pytest.py ...`)."
        )
    so_path = _ensure_compiled(pkg_dir, ext, sanitize=True)
    mod = _import_shared_module(so_path)
    if mod is None:
        raise RuntimeError(f"Failed to load sanitized _fastcore from {so_path}")
    return mod


def load_fastcore() -> Any:
    """Return compiled `_fastcore` C module if available, else None (pure-Python fallback)."""
    if os.environ.get("MOEX_PROOF_DISABLE_C") == "1":
        return None
    sanitize = os.environ.get("MOEX_FASTCORE_SANITIZE") == "1"
    if sanitize in _CACHE:
        return _CACHE[sanitize]
    with _LOCK:
        if sanitize in _CACHE:
            return _CACHE[sanitize]
        pkg_dir = Path(__file__).resolve().parent
        ext = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
        if sanitize:
            _CACHE[True] = _load_sanitized_fastcore(pkg_dir, ext)
            return _CACHE[True]
        # Any failure of the optional C extension falls back to pure Python, and the
        # negative result is cached so a broken build is not retried per call.
        loaded: Any = None
        try:
            so_path = _ensure_compiled(pkg_dir, ext, sanitize=False)
            loaded = _import_shared_module(so_path) if so_path.is_file() else None
        except (
            OSError,
            RuntimeError,
            ImportError,
            ValueError,
            AttributeError,
            TypeError,
            SystemError,
        ) as exc:
            warnings.warn(
                f"_fastcore unavailable ({exc}); falling back to pure Python.",
                RuntimeWarning,
                stacklevel=2,
            )
        _CACHE[False] = loaded
        return _CACHE[False]


def clean_obj(value: Any) -> Any:
    """Recursively replace non-finite floats with None via C99 ``fc.clean_obj`` when
    built, else the equivalent pure-Python ``math.isfinite`` fallback."""
    fc = load_fastcore()
    if fc is not None:
        return fc.clean_obj(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: clean_obj(item) for key, item in cast(dict[Any, Any], value).items()}
    if isinstance(value, (list, tuple)):
        return [clean_obj(item) for item in cast(list[Any] | tuple[Any, ...], value)]
    return value
