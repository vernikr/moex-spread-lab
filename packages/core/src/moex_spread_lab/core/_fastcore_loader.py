"""Lazy loader and atomic clang builder for the optional C99 `_fastcore` extension."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sysconfig
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_loaded = False
_module: Any = None


def load_fastcore() -> Any:
    """Return compiled `_fastcore` C module if available, else None (pure-Python fallback)."""
    global _loaded, _module
    if os.environ.get("MOEX_PROOF_DISABLE_C") == "1":
        return None
    if _loaded:
        return _module
    with _LOCK:
        if _loaded:
            return _module
        pkg_dir = Path(__file__).resolve().parent
        c_path = pkg_dir / "_fastcore.c"
        ext = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
        so_path = pkg_dir / f"_fastcore{ext}"
        try:
            need_build = c_path.is_file() and (
                not so_path.is_file() or so_path.stat().st_mtime_ns < c_path.stat().st_mtime_ns
            )
            if need_build:
                cc = shutil.which("clang") or shutil.which("cc")
                inc = sysconfig.get_path("include")
                if cc and inc and Path(inc, "Python.h").is_file():
                    tmp_so = pkg_dir / f"_fastcore.tmp.{os.getpid()}{ext}"
                    cmd = [
                        cc,
                        "-O3",
                        "-ffp-contract=off",
                        "-shared",
                        "-fPIC",
                        f"-I{inc}",
                        "-Wall",
                        "-Wno-unused-function",
                        str(c_path),
                        "-o",
                        str(tmp_so),
                        "-lm",
                    ]
                    if os.uname().sysname == "Darwin":
                        cmd.extend(["-undefined", "dynamic_lookup"])
                    subprocess.run(
                        cmd,
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    os.replace(tmp_so, so_path)
            if so_path.is_file():
                spec = importlib.util.spec_from_file_location(
                    "moex_spread_lab.core._fastcore", so_path
                )
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    _module = mod
        # code-health: allow broad exception: optional C extension falls back to Python.
        except Exception:
            _module = None
        _loaded = True
        return _module
