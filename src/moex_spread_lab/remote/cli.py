"""Authenticated workstation bridge client for status and fast-forward sync."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol, cast

from .workstation import patch_paths


class SubparserRegistry(Protocol):
    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser: ...


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def request(url: str, token: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 moex-spread-lab-remote/1",
    }
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=660) as response:
            result: Any = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeError(
            f"bridge request failed: {str(exc).replace(token, '[redacted]')}"
        ) from None
    if not isinstance(result, dict):
        raise RuntimeError("bridge returned invalid response")
    return cast(dict[str, Any], result)


def _candidate_options(include_patch: bool) -> dict[str, str]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    patch = (
        subprocess.run(
            ["git", "diff", "--binary", "HEAD"], capture_output=True, text=True, check=True
        ).stdout
        if include_patch
        else ""
    )
    if len(patch.encode()) > 16000:
        raise ValueError("tracked patch exceeds the 16 KB bridge limit")
    # Defense in depth: market data must never leave the workstation in a patch.
    for path in patch_paths(patch):
        if path == "data" or path == "data-lake" or path.startswith(("data/", "data-lake/")):
            raise ValueError(f"patch touches market data ({path}); refusing to send it")
    return {"revision": revision, "patch": patch}


def _workstation_command(args: argparse.Namespace, code: str) -> str:
    parts = ["python3", "-c", code, args.remote_command]
    root = os.environ.get("MOEX_WORKSTATION_ROOT")
    if args.remote_command == "probe":
        parts.append(root or "~/Downloads/projects/moex-spread-lab")
        payload = json.loads(args.json)
        settings = json.loads(args.settings)
        if not isinstance(payload, dict) or not isinstance(settings, dict):
            raise ValueError("--json and --settings must be JSON objects")
        parts.append(
            json.dumps(
                {
                    "action": args.action,
                    "payload": payload,
                    "settings_override": settings,
                    "profile": args.profile,
                    "sample_worker_rss": getattr(args, "sample_worker_rss", False),
                    "repeat": args.repeat,
                    "warmup": args.warmup,
                    "response_mode": "full" if args.full else "summary",
                }
            )
        )
    elif args.remote_command == "bench":
        parts.append(root or "~/Downloads/projects/moex-spread-lab")
        request_data = json.loads(args.json)
        if not isinstance(request_data, dict):
            raise ValueError("--json must be a JSON object")
        options: dict[str, Any] = {
            "suite": args.suite,
            "repeat": args.repeat,
            "warmup": args.warmup,
            "request": request_data,
        }
        if args.worktree_patch or args.suite == "ab_fastcore":
            options.update(_candidate_options(bool(args.worktree_patch)))
        parts.append(json.dumps(options))
    elif root:
        parts.append(root)
    return " ".join(shlex.quote(part) for part in parts)


def run(args: argparse.Namespace) -> int:
    token = os.environ.get("ARENA_BRIDGE_TOKEN", "")
    base = os.environ.get("ARENA_BRIDGE_URL", "").rstrip("/")
    url = urllib.parse.urlsplit(base)
    if (
        not token
        or url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        print(
            "Set ARENA_BRIDGE_URL to an HTTPS URL and ARENA_BRIDGE_TOKEN in the environment",
            file=sys.stderr,
        )
        return 2
    try:
        if args.remote_command == "status":
            health = request(base + "/health", token)
            if not health.get("ok"):
                raise RuntimeError("bridge health check failed")
        source = (Path(__file__).parent / "workstation.py").read_bytes()
        encoded = base64.b64encode(source).decode("ascii")
        code = f"import base64;exec(compile(base64.b64decode('{encoded}'),'<workstation>','exec'))"
        command = _workstation_command(args, code)
        result = request(
            base + "/exec",
            token,
            body={
                "command": command,
                "timeout_sec": 600 if args.remote_command == "bench" else 160,
            },
        )
        if not result.get("ok") or result.get("exit_code") != 0:
            detail = result.get("stderr") or result.get("error") or "workstation command failed"
            raise RuntimeError(str(detail)[:2000])
        payload = json.loads(result["stdout"])
        print(json.dumps(payload, sort_keys=True).replace(token, "[redacted]"))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(
            f"remote {args.remote_command}: {str(exc).replace(token, '[redacted]')}",
            file=sys.stderr,
        )
        return 1


def register(subparsers: SubparserRegistry) -> None:
    parser = subparsers.add_parser("remote", help="manage the authenticated workstation bridge")
    commands = parser.add_subparsers(dest="remote_command", required=True)
    commands.add_parser("status", help="show workstation checkout and server status")
    commands.add_parser("sync", help="fast-forward workstation main and restart its launch agent")
    probe = commands.add_parser("probe", help="run a non-persisting loopback diagnostic")
    probe.add_argument(
        "--action", required=True, choices=("catalog", "run", "optimize", "plateau", "windows")
    )
    probe.add_argument("--json", default="{}", help="proof request JSON object")
    probe.add_argument("--settings", default="{}", help="in-memory settings overrides")
    probe.add_argument("--repeat", type=int, default=1)
    probe.add_argument("--warmup", type=int, default=0)
    probe.add_argument("--profile", action="store_true")
    probe.add_argument(
        "--sample-worker-rss",
        action="store_true",
        help="sample aggregate worker RSS during the request",
    )
    probe.add_argument(
        "--full", action="store_true", help="return full result rather than compact summary"
    )
    bench = commands.add_parser(
        "bench", help="benchmark the workstation, optionally with an isolated tracked patch"
    )
    bench.add_argument(
        "--suite",
        required=True,
        choices=(
            "catalog_boot",
            "single_proof_1d_1h",
            "calendar_proof",
            "window_table_batch",
            "cohort_optimize",
            "ab_fastcore",
            "custom",
        ),
    )
    bench.add_argument("--repeat", type=int, default=1)
    bench.add_argument(
        "--warmup", type=int, default=1, help="warm-up calls per workload (default: 1)"
    )
    bench.add_argument(
        "--json", default="{}", help="custom action/payload/settings_override object"
    )
    bench.add_argument(
        "--worktree-patch",
        choices=("HEAD",),
        help="compare against current tracked Git diff; untracked files are excluded",
    )
    parser.set_defaults(_msl_cli_handler=run)
