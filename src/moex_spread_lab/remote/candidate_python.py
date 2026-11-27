"""Candidate interpreter selection for bench worktrees.

Keeps ``remote.bench`` under the token ceiling; the trust decision
(reuse root ``.venv`` with ``PYTHONPATH`` shadowing vs. isolated sync) lives
here next to the verification that the candidate actually shadows the root.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .workstation import is_dependency_input

_NATIVE_BUILD_SUFFIXES = (".c", ".h", ".pyx", ".pxd")


def deps_inputs_touched(paths: list[str]) -> bool:
    return any(is_dependency_input(path) or path.endswith(_NATIVE_BUILD_SUFFIXES) for path in paths)


def candidate_pythonpath(path: Path) -> str:
    entries = [str(path / "src")]
    entries.extend(str(src) for src in sorted(path.glob("packages/*/src")))
    return os.pathsep.join(entries)


def assert_candidate_python(python: Path, pythonpath: str, path: Path) -> None:
    code = "import moex_spread_lab.core.w0 as m; print(m.__file__)"
    env = {**os.environ, "PYTHONPATH": pythonpath, "MOEX_OFFLINE_ONLY": "1"}
    result = subprocess.run(
        [str(python), "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if result.returncode:
        raise RuntimeError(f"candidate interpreter failed: {result.stderr[-500:]}")
    loaded = Path(result.stdout.strip()).resolve()
    try:
        loaded.relative_to(path.resolve())
    except ValueError:
        raise RuntimeError(
            f"candidate interpreter loaded {loaded}, which is not inside {path}"
        ) from None


@dataclass
class CandidateWorktree:
    path: Path
    python: Path
    pythonpath: str | None
    data_mode: str
    isolated_venv: bool
