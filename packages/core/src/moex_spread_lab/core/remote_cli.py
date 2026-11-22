"""Authenticated workstation bridge client for status and fast-forward sync."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol, cast


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
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=180) as response:
            result: Any = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeError(
            f"bridge request failed: {str(exc).replace(token, '[redacted]')}"
        ) from None
    if not isinstance(result, dict):
        raise RuntimeError("bridge returned invalid response")
    return cast(dict[str, Any], result)


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
        source = (Path(__file__).parent / "workstation_remote.py").read_bytes()
        encoded = base64.b64encode(source).decode("ascii")
        code = f"import base64;exec(compile(base64.b64decode('{encoded}'),'<workstation_remote>','exec'))"
        parts = ["python3", "-c", code, args.remote_command]
        root = os.environ.get("MOEX_WORKSTATION_ROOT")
        if root:
            parts.append(root)
        command = " ".join(shlex.quote(part) for part in parts)
        result = request(base + "/exec", token, body={"command": command, "timeout_sec": 160})
        if not result.get("ok") or result.get("exit_code") != 0:
            detail = result.get("stderr") or result.get("error") or "workstation command failed"
            raise RuntimeError(str(detail)[:2000])
        payload = json.loads(result["stdout"])
        print(json.dumps(payload, sort_keys=True).replace(token, "[redacted]"))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
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
    parser.set_defaults(_msl_cli_handler=run)
