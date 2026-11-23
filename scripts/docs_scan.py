#!/usr/bin/env python3
"""Docs-file integrity scan (plan S2 docs profile).

Checks changed Markdown/docs files for trailing whitespace, unresolved merge
conflict markers and credential-shaped tokens. Stdlib only; used by
``scripts/verify.py --profile affected`` for docs-only changes and runnable
standalone. Exit 0 = clean, 1 = findings, 2 = usage error.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 2_000_000
_CREDENTIAL_RE = re.compile(
    r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{10,}"
    r"|AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9._~+/-]{16,})"
)
_CONFLICT_MARKERS = ("<<<<<<<", ">>>>>>>", "|||||||")


def _norm(path: str) -> str:
    return path.strip().replace("\\", "/").removeprefix("./").lstrip("/")


def _resolve(raw: str) -> tuple[Path, str]:
    stripped = raw.strip()
    if Path(stripped).is_absolute():
        return Path(stripped), stripped
    display = _norm(stripped)
    return ROOT / display, display


def _scan_text(display: str, text: str) -> list[str]:
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.rstrip() != line:
            findings.append(f"{display}:{lineno}: trailing whitespace")
        if line.lstrip().startswith(_CONFLICT_MARKERS):
            findings.append(f"{display}:{lineno}: conflict marker")
        if len(findings) >= 20:
            findings.append("... (truncated at 20 findings)")
            return findings
    credential = _CREDENTIAL_RE.search(text)
    if credential:
        findings.append(f"{display}: credential-shaped token ({credential.group(0)[:12]}...)")
    return findings


def scan_docs_files(paths: list[str]) -> tuple[list[str], list[str]]:
    """Return ``(findings, notes)`` for the given paths (``path[:line]: message``)."""
    findings: list[str] = []
    notes: list[str] = []
    for raw in paths:
        path, display = _resolve(raw)
        if not path.is_file():
            notes.append(f"{display}: not on disk (deleted?); skipped")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            notes.append(f"{display}: unreadable ({exc}); skipped")
            continue
        if len(data) > MAX_BYTES:
            notes.append(f"{display}: larger than 2 MB; skipped")
            continue
        findings.extend(_scan_text(display, data.decode("utf-8", errors="replace")))
    return findings, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="docs files to scan")
    args = parser.parse_args(argv)
    findings, notes = scan_docs_files(args.paths)
    for note in notes:
        print(f"docs-scan: note: {note}")
    for finding in findings:
        print(f"docs-scan: {finding}")
    if findings:
        print(f"docs-scan: {len(findings)} finding(s)")
        return 1
    print("docs-scan: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
