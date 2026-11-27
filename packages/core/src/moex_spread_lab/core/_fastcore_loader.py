"""Lazy loader and atomic clang builder for the optional C99 `_fastcore` extension."""

from __future__ import annotations

import ctypes
import importlib.util
import math
import os
import shutil
import subprocess
import sysconfig
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

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


def _ensure_compiled(pkg_dir: Path, ext: str, *, sanitize: bool) -> Path:
    stem = "_fastcore_asan" if sanitize else "_fastcore"
    c_path = pkg_dir / "_fastcore.c"
    so_path = pkg_dir / f"{stem}{ext}"
    need_build = c_path.is_file() and (
        not so_path.is_file() or so_path.stat().st_mtime_ns < c_path.stat().st_mtime_ns
    )
    if not need_build:
        return so_path
    cc = shutil.which("clang") or (None if sanitize else shutil.which("cc"))
    inc = sysconfig.get_path("include")
    if not (cc and inc and Path(inc, "Python.h").is_file()):
        if sanitize:
            raise RuntimeError(
                "MOEX_FASTCORE_SANITIZE=1 requires clang and Python development headers"
            )
        return so_path
    tmp_so = pkg_dir / f"{stem}.tmp.{os.getpid()}{ext}"
    cmd = _build_compile_command(cc, inc, c_path, tmp_so, sanitize=sanitize)
    try:
        res = subprocess.run(cmd, env=_compiler_env(), check=False, capture_output=True, text=True)
        if res.returncode != 0:
            if sanitize:
                raise RuntimeError(
                    f"Sanitized _fastcore build failed ({res.returncode}): {res.stderr.strip()}"
                )
            return so_path
        os.replace(tmp_so, so_path)
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
        with suppress(Exception):
            so_path = _ensure_compiled(pkg_dir, ext, sanitize=False)
            loaded = _import_shared_module(so_path) if so_path.is_file() else None
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
