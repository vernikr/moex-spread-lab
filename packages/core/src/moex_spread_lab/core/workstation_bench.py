"""Offline benchmark helpers for a workstation and disposable candidate worktree.

Candidate data is copied, not linked, so candidate code cannot mutate the live data
through the benchmark root. The candidate is trusted code, not a security sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import shutil
import signal
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from .w0 import canonical_bytes, sha256_bytes


def _cmd(root: Path, *args: str, input_text: str | None = None) -> str:
    proc = subprocess.run(
        args, cwd=root, input=input_text, text=True, capture_output=True, check=False
    )
    if proc.returncode:
        raise RuntimeError(f"{args[0]} failed ({proc.returncode}): {proc.stderr[-1000:]}")
    return proc.stdout.strip()


def _post(port: int, payload: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/dev/probe",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            value: dict[str, Any] = json.load(response)
            return _compact(value)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"benchmark HTTP {exc.code}: {exc.read(1000).decode(errors='replace')}"
        ) from None


def _compact(value: dict[str, Any]) -> dict[str, Any]:
    full: Any = value["result"]
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


def _relocate_copied_seal(root: Path, candidate: Path) -> None:
    """Rebind only an integrity-checked private seal copy to its temporary root."""
    seal = candidate / "data-lake/h1-seal.json"
    marker = candidate / "data-lake/h1-seal.enabled.json"
    if not seal.exists() and not marker.exists():
        return
    if not seal.is_file() or not marker.is_file():
        raise RuntimeError("incomplete H1 seal; refusing candidate benchmark")
    value = json.loads(seal.read_text(encoding="utf-8"))
    activation = json.loads(marker.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(activation, dict):
        raise RuntimeError("invalid source H1 seal; refusing candidate benchmark")
    value = cast(dict[str, Any], value)
    activation = cast(dict[str, Any], activation)
    if (
        value.get("root") != str(root.resolve())
        or value.get("schema_version") != "h1-sealed-read.v1"
        or activation.get("schema_version") != value.get("schema_version")
        or activation.get("seal_sha256") != sha256_bytes(canonical_bytes(value))
    ):
        raise RuntimeError("invalid source H1 seal; refusing candidate benchmark")
    value["root"] = str(candidate.resolve())
    activation["seal_sha256"] = sha256_bytes(canonical_bytes(value))
    seal.write_text(json.dumps(value, sort_keys=True, indent=2), encoding="utf-8")
    marker.write_text(json.dumps(activation, sort_keys=True, indent=2), encoding="utf-8")


def _copy_data(root: Path, candidate: Path) -> None:
    for name in ("data", "data-lake"):
        src = root / name
        if not src.exists():
            continue
        if src.is_symlink() or any(path.is_symlink() for path in src.rglob("*")):
            raise RuntimeError(f"{name} contains symlinks; refusing to copy external targets")
        ignored = shutil.ignore_patterns("analytics") if name == "data" else None
        shutil.copytree(src, candidate / name, ignore=ignored)
    original = root / "data/analytics/research-settings.json"
    if original.is_file():
        target = candidate / "data/analytics/research-settings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)


@contextmanager
def candidate_worktree(root: Path, revision: str, patch: str) -> Generator[Path]:
    with tempfile.TemporaryDirectory(prefix="moex-bench-") as directory:
        candidate = Path(directory) / "checkout"
        _cmd(root, "git", "worktree", "add", "--detach", str(candidate), revision)
        try:
            if patch:
                _cmd(candidate, "git", "apply", "--binary", "-", input_text=patch)
            _copy_data(root, candidate)
            _relocate_copied_seal(root, candidate)
            _cmd(candidate, "uv", "sync", "--frozen")
            yield candidate
        finally:
            _cmd(root, "git", "worktree", "remove", "--force", str(candidate))


@contextmanager
def candidate_server(candidate: Path) -> Generator[int]:
    env = dict(os.environ, MOEX_OFFLINE_ONLY="1")
    with tempfile.TemporaryFile(mode="w+") as stderr:
        process = subprocess.Popen(
            [
                str(candidate / ".venv/bin/python3"),
                "-m",
                "moex_spread_lab.cli",
                "screener",
                "--root",
                str(candidate),
                "--port",
                "0",
            ],
            cwd=candidate,
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


def _suite(name: str, request: dict[str, Any], repeat: int, warmup: int) -> list[dict[str, Any]]:
    pair = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"}
    calendar = {
        "universe": "calendar_native",
        "asset": "SR",
        "pair": "SRU6/SRZ6",
        "strategy": "zscore_mean_reversion",
    }
    jobs: dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]] = {
        "catalog_boot": [("catalog", {}, {"timeframe": tf}) for tf in ("1d", "1h")],
        "single_proof_1d_1h": [("run", pair, {"timeframe": tf}) for tf in ("1d", "1h")],
        "calendar_proof": [("run", calendar, {"timeframe": "1d"})],
        "window_table_batch": [("windows", {"requests": [pair]}, {"timeframe": "1d"})],
        "cohort_optimize": [("optimize", pair, {"timeframe": "1d", "random_seed": 41})],
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


def _health() -> dict[str, Any]:
    with urllib.request.urlopen("http://127.0.0.1:8765/api/dev/health", timeout=5) as response:
        health: dict[str, Any] = json.load(response)
    return health


def benchmark(root: Path, options: dict[str, Any]) -> dict[str, Any]:
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
    baseline = [_post(8765, job) for job in jobs]
    result: dict[str, Any] = {
        "suite": suite,
        "warmup": warmup,
        "loadavg_1m": health.get("loadavg_1m"),
        "baseline_cache_state": "live server (possibly warm)",
        "candidate_cache_state": "new server",
        "baseline": baseline,
    }
    if revision:
        with (
            candidate_worktree(root, revision, options.get("patch", "")) as candidate,
            candidate_server(candidate) as port,
        ):
            comparison = [_post(port, job) for job in jobs]
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
