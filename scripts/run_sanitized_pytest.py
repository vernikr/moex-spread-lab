#!/usr/bin/env python3
"""Run pytest with C99 `_fastcore` compiled and preloaded under AddressSanitizer + UBSan."""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Sanitizer gate is full-resolution: default targets cover every equivalence tier.
DEFAULT_TARGETS = (
    "packages/proof/tests/test_perf_equivalence.py",
    "packages/proof/tests/test_perf_equivalence_pair.py",
    "packages/proof/tests/test_perf_equivalence_pool.py",
    "packages/proof/tests/test_perf_equivalence_stream.py",
    "packages/proof/tests/test_perf_equivalence_full.py",
)

# Suppress only interpreter-lifetime allocators so LSan stays armed for _fastcore leaks.
LSAN_SUPPRESSIONS = """\
leak:_PyObject_Malloc
leak:_PyObject_Realloc
leak:_PyObject_Calloc
leak:_PyMem_RawMalloc
leak:_PyMem_RawRealloc
leak:_PyMem_RawCalloc
leak:_PyUnicodeWriter_Finish
leak:PyThread_allocate_lock
leak:_duckdb
leak:_multiarray_umath
leak:_mt19937
leak:libarrow
leak:pyarrow
"""


def _machine_candidates(machine: str) -> tuple[str, ...]:
    norm = machine.lower()
    if norm in {"x86_64", "amd64"}:
        return ("x86_64",)
    if norm in {"aarch64", "arm64"}:
        return ("aarch64", "arm64")
    return (machine,)


def _candidate_runtime_names(sysname: str, machine: str) -> tuple[str, ...]:
    if sysname == "Darwin":
        return ("libclang_rt.asan_osx_dynamic.dylib",)
    return (
        *(f"libclang_rt.asan-{arch}.so" for arch in _machine_candidates(machine)),
        "libclang_rt.asan.so",
    )


def _probe_clang_file(cc: str, name: str) -> Path | None:
    res = subprocess.run(
        [cc, f"-print-file-name={name}"], check=False, capture_output=True, text=True
    )
    candidate = Path(res.stdout.strip())
    if res.returncode == 0 and candidate.is_absolute() and candidate.is_file():
        return candidate.resolve()
    return None


def _probe_resource_dir(cc: str, names: tuple[str, ...]) -> Path | None:
    res = subprocess.run([cc, "-print-resource-dir"], check=False, capture_output=True, text=True)
    lib_root = Path(res.stdout.strip()) / "lib"
    if res.returncode != 0 or not lib_root.is_dir():
        return None
    for name in names:
        matches = sorted(lib_root.rglob(name))
        if matches:
            return matches[0].resolve()
    return None


def discover_asan_runtime(clang_bin: str | None = None) -> Path:
    cc = clang_bin or shutil.which("clang")
    if not cc:
        raise RuntimeError("clang was not found on PATH; cannot locate ASan runtime")
    names = _candidate_runtime_names(platform.system(), platform.machine())
    for name in names:
        found = _probe_clang_file(cc, name)
        if found is not None:
            return found
    found = _probe_resource_dir(cc, names)
    if found is not None:
        return found
    raise RuntimeError(
        f"Could not find clang dynamic ASan runtime ({', '.join(names)}); "
        "install libclang-rt-<ver>-dev on Linux or Xcode Command Line Tools on macOS."
    )


def build_sanitized_env(
    asan_runtime: Path,
    lsan_supp_path: Path | None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env["MOEX_FASTCORE_SANITIZE"] = "1"
    env["MOEX_FASTCORE_ASAN_RUNTIME"] = str(asan_runtime)
    env.pop("MOEX_PROOF_DISABLE_C", None)
    insert_var = "DYLD_INSERT_LIBRARIES" if platform.system() == "Darwin" else "LD_PRELOAD"
    existing = env.get(insert_var, "")
    env[insert_var] = f"{asan_runtime}:{existing}" if existing else str(asan_runtime)
    darwin = insert_var == "DYLD_INSERT_LIBRARIES"
    env.setdefault(
        "ASAN_OPTIONS",
        "detect_leaks=0:halt_on_error=1:abort_on_error=1"
        if darwin
        else "detect_leaks=1:halt_on_error=1",
    )
    if not darwin and lsan_supp_path is not None:
        env.setdefault("LSAN_OPTIONS", f"suppressions={lsan_supp_path}")
    env.setdefault("UBSAN_OPTIONS", "print_stacktrace=1:halt_on_error=1")
    return env


def _deselection_overridden(args: list[str]) -> bool:
    """``-m ""`` (or any ``-m`` value) overrides pytest's ``addopts`` expression."""
    return any(a == "-m" or a.startswith("-m=") or a == "--markers" for a in args)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not any(not a.startswith("-") for a in args):
        args.extend(DEFAULT_TARGETS)
    if not _deselection_overridden(args):
        # pyproject deselects slow_equiv by default; a sanitizer run is full-resolution.
        args = ["-m", "", *args]
    asan_runtime = discover_asan_runtime()
    with tempfile.TemporaryDirectory(prefix="moex_fastcore_san_") as tmp_dir:
        supp_path = Path(tmp_dir) / "lsan.supp"
        supp_path.write_text(LSAN_SUPPRESSIONS, encoding="utf-8")
        env = build_sanitized_env(asan_runtime, supp_path)
        bootstrap = (
            "import os, pytest, sys; "
            "rt = os.environ.get('MOEX_FASTCORE_ASAN_RUNTIME'); "
            "os.environ['DYLD_INSERT_LIBRARIES'] = rt if (sys.platform == 'darwin' and rt and not os.environ.get('DYLD_INSERT_LIBRARIES')) else os.environ.get('DYLD_INSERT_LIBRARIES', ''); "
            "raise SystemExit(pytest.main(sys.argv[1:]))"
        )
        # sys.executable bypasses macOS SIP stripping; DYLD_* is restored in-process
        # so multiprocessing spawn workers preload ASan too.
        cmd = [sys.executable, "-c", bootstrap, *args]
        proc = subprocess.run(cmd, cwd=ROOT, env=env, check=False)
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
