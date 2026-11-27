"""Offline benchmark helpers for a workstation and disposable candidate worktree.

Candidate data is provisioned privately by default (APFS copy-on-write clone on
macOS, a byte copy elsewhere), so candidate code cannot mutate the live data
through the benchmark root. A read-only-by-convention symlink farm exists as an
explicit opt-in (``MOEX_BENCH_DATA=symlink``). The candidate is trusted code, not
a security sandbox; the trust boundary is documented in ``remote.candidate_data``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import select
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, cast

from .candidate_data import provision_data
from .candidate_data import relocate_copied_seal as _relocate_copied_seal
from .candidate_python import (
    CandidateWorktree,
    assert_candidate_python,
    candidate_pythonpath,
    deps_inputs_touched,
)
from .workstation import patch_paths as _patch_paths


def normalize_provenance(full: Any, root: Path | None) -> Any:
    """Rewrite root-bound provenance paths so isolated worktree results compare equal."""
    if root is None or not isinstance(full, dict):
        return full
    mapping = cast(dict[str, Any], full)
    raw: Any = mapping.get("provenance")
    if not isinstance(raw, dict):
        return mapping
    provenance = dict(cast(dict[str, Any], raw))
    for key, entry in provenance.items():
        if key.endswith("_path") and isinstance(entry, str) and Path(entry).is_absolute():
            # External paths stay unchanged, and cannot compare equal accidentally.
            with suppress(ValueError):
                provenance[key] = "$ROOT/" + str(Path(entry).resolve().relative_to(root.resolve()))
    return {**mapping, "provenance": provenance}


def _cmd(root: Path, *args: str, input_text: str | None = None) -> str:
    proc = subprocess.run(
        args, cwd=root, input=input_text, text=True, capture_output=True, check=False
    )
    if proc.returncode:
        raise RuntimeError(f"{args[0]} failed ({proc.returncode}): {proc.stderr[-1000:]}")
    return proc.stdout.strip()


def _post(port: int, payload: dict[str, Any], root: Path) -> dict[str, Any]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/dev/probe",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            value: dict[str, Any] = json.load(response)
            return _compact(value, root)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"benchmark HTTP {exc.code}: {exc.read(1000).decode(errors='replace')}"
        ) from None


def _compact(value: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    full: Any = value["result"]
    full = normalize_provenance(full, root)
    digest = hashlib.sha256(
        json.dumps(full, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    mapping = cast(dict[str, Any], full) if isinstance(full, dict) else {}
    summary: Any = mapping.get("summary", {})
    result = {"sha256": digest}
    if isinstance(full, dict):
        for key in ("run_id", "rank_score", "seed_used", "trial_count"):
            if key in full:
                result[key] = full[key]
    if isinstance(summary, dict) and "ledger_reconciliation_rub" in summary:
        result["ledger_reconciliation_rub"] = cast(dict[str, Any], summary)[
            "ledger_reconciliation_rub"
        ]
    return {"action": value["action"], "result": result, "telemetry": value["telemetry"]}


@contextmanager
def candidate_worktree(root: Path, revision: str, patch: str) -> Generator[CandidateWorktree]:
    """Detached worktree + provisioned data + a ready-to-import candidate.

    Reuses the root ``.venv`` with ``PYTHONPATH`` shadowing when the patch does
    not touch dependency or native-build inputs; falls back to an isolated
    ``uv sync --frozen`` candidate environment otherwise.
    """
    with tempfile.TemporaryDirectory(prefix="moex-bench-") as directory:
        path = Path(directory) / "checkout"
        _cmd(root, "git", "worktree", "add", "--detach", str(path), revision)
        try:
            if patch:
                _cmd(path, "git", "apply", "--binary", "-", input_text=patch)
            data_mode = provision_data(root, path)
            _relocate_copied_seal(root, path)
            if deps_inputs_touched(_patch_paths(patch)):
                _cmd(path, "uv", "sync", "--frozen")
                candidate = CandidateWorktree(
                    path=path,
                    python=path / ".venv/bin/python3",
                    pythonpath=None,
                    data_mode=data_mode,
                    isolated_venv=True,
                )
            else:
                python = root / ".venv/bin/python3"
                pythonpath = candidate_pythonpath(path)
                assert_candidate_python(python, pythonpath, path)
                candidate = CandidateWorktree(
                    path=path,
                    python=python,
                    pythonpath=pythonpath,
                    data_mode=data_mode,
                    isolated_venv=False,
                )
            yield candidate
        finally:
            _cmd(root, "git", "worktree", "remove", "--force", str(path))


@contextmanager
def candidate_server(
    candidate: CandidateWorktree, overrides: dict[str, str] | None = None
) -> Generator[int]:
    env = dict(os.environ, MOEX_OFFLINE_ONLY="1")
    if candidate.pythonpath:
        env["PYTHONPATH"] = candidate.pythonpath
    if overrides is not None:
        env.update(overrides)
    with tempfile.TemporaryFile(mode="w+") as stderr:
        process = subprocess.Popen(
            [
                str(candidate.python),
                "-m",
                "moex_spread_lab.cli",
                "screener",
                "--root",
                str(candidate.path),
                "--port",
                "0",
            ],
            cwd=candidate.path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        try:
            assert process.stdout is not None
            ready, _, _ = select.select([process.stdout], [], [], 60)
            if not ready:
                raise RuntimeError("candidate server failed to start within 60 seconds")
            line = process.stdout.readline().strip()
            if not line.startswith("Local research screener: http://127.0.0.1:"):
                raise RuntimeError("candidate server failed to start: " + line[:200])
            yield int(line.rsplit(":", 1)[1])
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            if process.stdout:
                process.stdout.close()


def _health() -> dict[str, Any]:
    with urllib.request.urlopen("http://127.0.0.1:8765/api/dev/health", timeout=5) as response:
        health: dict[str, Any] = json.load(response)
    return health


@contextmanager
def benchmark_lock(root: Path) -> Generator[None]:
    """Reject concurrent benchmark runs on the same workstation checkout."""
    cache = Path.home() / ".cache/moex-spread-lab/bench-locks"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(str(root.resolve()).encode()).hexdigest() + ".lock"
    with (cache / name).open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another benchmark is running for this checkout") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def benchmark(root: Path, options: dict[str, Any]) -> dict[str, Any]:
    with benchmark_lock(root):
        return _benchmark(root, options)


def _benchmark(root: Path, options: dict[str, Any]) -> dict[str, Any]:
    suite, repeat = options["suite"], options.get("repeat", 1)
    if type(repeat) is not int or not 1 <= repeat <= 10:
        raise ValueError("repeat must be between 1 and 10")
    warmup = options.get("warmup", 1)
    if type(warmup) is not int or not 0 <= warmup <= 3:
        raise ValueError("warmup must be between 0 and 3")
    jobs = _suite(suite, options.get("request", {}), repeat, warmup)
    health = _health()
    if any(health.get("locks", {}).values()):
        raise RuntimeError("workstation server is busy; benchmark refused")
    revision = options.get("revision")
    if revision and _cmd(root, "git", "rev-parse", "HEAD") != revision:
        raise RuntimeError("workstation HEAD differs from candidate base; run remote sync first")
    if suite == "ab_fastcore":
        if not revision:
            raise ValueError("ab_fastcore requires a candidate revision")
        return _benchmark_fastcore(root, revision, options.get("patch", ""), jobs, health, warmup)
    baseline = [_post(8765, job, root) for job in jobs]
    result: dict[str, Any] = {
        "suite": suite,
        "warmup": warmup,
        "loadavg_1m": health.get("loadavg_1m"),
        "baseline_cache_state": "live server (possibly warm)",
        "candidate_cache_state": "new server",
        "baseline": baseline,
    }
    if revision:
        setup_start = time.perf_counter()
        with (
            candidate_worktree(root, revision, options.get("patch", "")) as candidate,
            candidate_server(candidate) as port,
        ):
            result["candidate_setup_ms"] = round((time.perf_counter() - setup_start) * 1000, 1)
            result["candidate_data_mode"] = candidate.data_mode
            result["candidate_isolated_venv"] = candidate.isolated_venv
            comparison = [_post(port, job, candidate.path) for job in jobs]
        result["candidate"] = comparison
        result["exact_match"] = [a["result"] for a in baseline] == [b["result"] for b in comparison]
        result["speedup_x"] = [
            round(a["telemetry"]["median_ms"] / b["telemetry"]["median_ms"], 3)
            if b["telemetry"]["median_ms"]
            else None
            for a, b in zip(baseline, comparison, strict=True)
        ]
        if not result["exact_match"]:
            raise RuntimeError("candidate benchmark results differ from baseline")
    return result


def _benchmark_fastcore(
    root: Path,
    revision: str,
    patch: str,
    jobs: list[dict[str, Any]],
    health: dict[str, Any],
    warmup: int,
) -> dict[str, Any]:
    # Both servers use the same isolated copied checkout, never the live daemon.
    # Explicit worker counts avoid inheriting ambient user settings.
    setup_start = time.perf_counter()
    with candidate_worktree(root, revision, patch) as candidate:
        setup_ms = round((time.perf_counter() - setup_start) * 1000, 1)
        with candidate_server(
            candidate, {"MOEX_PROOF_DISABLE_C": "0", "MOEX_PROOF_WORKERS": "6"}
        ) as port:
            baseline = [_post(port, job, candidate.path) for job in jobs]
        with candidate_server(
            candidate, {"MOEX_PROOF_DISABLE_C": "1", "MOEX_PROOF_WORKERS": "1"}
        ) as port:
            comparison = [_post(port, job, candidate.path) for job in jobs]
    exact = [a["result"] for a in baseline] == [b["result"] for b in comparison]
    if not exact:
        raise RuntimeError("fastcore A/B benchmark results differ")
    return {
        "suite": "ab_fastcore",
        "warmup": warmup,
        "loadavg_1m": health.get("loadavg_1m"),
        "candidate_setup_ms": setup_ms,
        "candidate_data_mode": candidate.data_mode,
        "candidate_isolated_venv": candidate.isolated_venv,
        "baseline": baseline,
        "candidate": comparison,
        "exact_match": exact,
        "baseline_mode": "C enabled, 6 workers",
        "candidate_mode": "C disabled, 1 worker",
        "speedup_x": [
            round(b["telemetry"]["median_ms"] / a["telemetry"]["median_ms"], 3)
            if a["telemetry"]["median_ms"]
            else None
            for a, b in zip(baseline, comparison, strict=True)
        ],
    }


def _suite(name: str, request: dict[str, Any], repeat: int, warmup: int) -> list[dict[str, Any]]:
    pair = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"}
    calendar = {
        "universe": "calendar_native",
        "asset": "SR",
        "pair": "SRU6/SRZ6",
        "strategy": "zscore_mean_reversion",
    }
    si_pairs = (
        "SiZ3/SiH4",
        "SiH4/SiM4",
        "SiM4/SiU4",
        "SiU4/SiZ4",
        "SiZ4/SiH5",
        "SiH5/SiM5",
        "SiM5/SiU5",
        "SiU5/SiZ5",
        "SiZ5/SiH6",
        "SiH6/SiM6",
    )
    targets = [{"universe": "calendar_native", "asset": "Si", "pair": p} for p in si_pairs]
    si_windows = [{**t, "strategy": "zscore_mean_reversion"} for t in targets]
    cohort = {**si_windows[-1], "optimize_strategy": True, "targets": targets}
    pair_sized = {**pair, "sizing": "max_affordable"}
    gold = {**pair_sized, "pair": "GLDRUBF/GLZ6"}
    jobs: dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]] = {
        "catalog_boot": [("catalog", {}, {"timeframe": tf}) for tf in ("1d", "1h")],
        "single_proof_1d_1h": [
            ("run", item, {"timeframe": tf}) for item in (pair_sized, gold) for tf in ("1d", "1h")
        ],
        "calendar_proof": [
            ("run", {**calendar, "strategy": strategy}, {"timeframe": "1d"})
            for strategy in ("zscore_mean_reversion", "convergence_to_ruonia")
        ],
        "window_table_batch": [("windows", {"requests": si_windows}, {"timeframe": "1d"})],
        "cohort_optimize": [("optimize", cohort, {"timeframe": "1d", "random_seed": 41})],
        "ab_fastcore": [("optimize", cohort, {"timeframe": "1d", "random_seed": 41})],
        "custom": [
            (
                str(request.get("action", "catalog")),
                request.get("payload", {}),
                request.get("settings_override", {}),
            )
        ],
    }
    if name not in jobs:
        raise ValueError("unknown benchmark suite")
    return [
        {
            "action": action,
            "payload": payload,
            "settings_override": {"random_seed": 41, **settings},
            "repeat": repeat,
            "warmup": warmup,
            "response_mode": "full",
        }
        for action, payload, settings in jobs[name]
    ]


def main() -> int:
    import sys

    def terminate(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        print(json.dumps(benchmark(Path.cwd(), json.loads(sys.argv[1])), sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
