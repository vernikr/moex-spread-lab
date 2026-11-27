#!/usr/bin/env python3
"""Docs-file integrity scan (plan S2 docs profile).

Checks changed Markdown/docs for trailing whitespace, conflict markers and
credential-shaped tokens. Exit 0 = clean, 1 = findings, 2 = usage error.
``--links`` verifies relative links/anchors; ``--repo --links`` checks the
authored docs surface and skips the style scan (changed files only).
Preserved evidence is excluded from link checking because it is reproduced
as received: ``docs/inputs/`` (pinned by ``docs/inputs/manifest.json``),
``docs/research/results/received/`` and ``docs/research/reuse/``."""

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 2_000_000
_CREDENTIAL_RE = re.compile(
    r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{10,}"
    r"|AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9._~+/-]{16,})"
)
_CONFLICT_MARKERS = ("<<<<<<<", ">>>>>>>", "|||||||")
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
# Verbatim user inputs: pinned by docs/inputs/manifest.json, never edited.
_EXCLUDED_PREFIXES = (
    "docs/inputs/",
    "docs/research/results/received/",
    "docs/research/reuse/",
)
_REPO_DOC_GLOBS = (
    "README.md",
    "AGENTS.md",
    "docs/**/*.md",
    "packages/*/docs/**/*.md",
)


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


def repo_doc_paths() -> list[str]:
    found: list[str] = []
    for pattern in _REPO_DOC_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            display = path.relative_to(ROOT).as_posix()
            if path.is_file() and not display.startswith(_EXCLUDED_PREFIXES):
                found.append(display)
    return list(dict.fromkeys(found))


def _anchor_slug(heading: str) -> str:
    slug = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return slug.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {_anchor_slug(match.group(1)) for match in _HEADING_RE.finditer(text)}


def _is_external(target: str) -> bool:
    return target.startswith(("http://", "https://", "mailto:"))


def _resolve_link(path: Path, location: str) -> Path:
    return path if not location else (path.parent / location).resolve()


def _anchor_missing(cache: dict[Path, set[str]], resolved: Path, anchor: str) -> bool:
    if resolved not in cache:
        cache[resolved] = _anchors(resolved) if resolved.is_file() else set()
    return anchor.lower() not in cache[resolved]


def _link_findings(display: str, path: Path, text: str) -> list[str]:
    findings: list[str] = []
    anchors: dict[Path, set[str]] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in _LINK_RE.finditer(line):
            target = match.group(1)
            if _is_external(target):
                continue
            location, _, anchor = target.partition("#")
            resolved = _resolve_link(path, location)
            if not resolved.exists():
                findings.append(f"{display}:{lineno}: unresolved link target {target}")
            elif anchor and _anchor_missing(anchors, resolved, anchor):
                findings.append(f"{display}:{lineno}: unresolved link anchor {target}")
    return findings


def scan_docs_files(paths: list[str]) -> tuple[list[str], list[str]]:
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


def scan_links(paths: list[str]) -> tuple[list[str], list[str]]:
    findings: list[str] = []
    notes: list[str] = []
    for raw in paths:
        path, display = _resolve(raw)
        if display.startswith(_EXCLUDED_PREFIXES):
            notes.append(f"{display}: preserved as received; links not checked")
            continue
        if not path.is_file():
            notes.append(f"{display}: not on disk (deleted?); skipped")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            notes.append(f"{display}: unreadable ({exc}); skipped")
            continue
        findings.extend(_link_findings(display, path, text))
    return findings, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="docs files to scan")
    parser.add_argument("--repo", action="store_true", help="scan repo-wide docs surface")
    parser.add_argument("--links", action="store_true", help="also verify links and anchors")
    args = parser.parse_args(argv)
    if args.repo:
        args.links = True  # authored surface: link integrity only, never restyled
        paths = repo_doc_paths()
        findings, notes = [], []
    else:
        paths = list(args.paths)
        if not paths:
            parser.error("no paths given; pass files or --repo")
        findings, notes = scan_docs_files(paths)
    if args.links:
        link_findings, link_notes = scan_links(paths)
        findings.extend(link_findings)
        notes.extend(link_notes)
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
