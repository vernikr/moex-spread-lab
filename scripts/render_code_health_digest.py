#!/usr/bin/env python3
"""Collect and render the weekly Tier 3 code-health, hotspot, coupling, and duplication digest."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__:
    from .code_health_digest_format import render_digest_markdown
    from .code_health_gate_common import ROOT, _file_metrics, _relative, _source_files
else:
    from code_health_digest_format import render_digest_markdown
    from code_health_gate_common import ROOT, _file_metrics, _relative, _source_files

DEFAULT_OUT = ROOT / "reports/code-health/code-health-digest.md"
DEFAULT_JSON_OUT = ROOT / "reports/code-health/code-health-digest.json"
DEFAULT_JSCPD_REPORT = ROOT / "reports/code-health/jscpd/jscpd-report.json"
DEFAULT_REPOWISE_DB = ROOT / ".repowise/wiki.db"
DEFAULT_REPOWISE_HEALTH = ROOT / "reports/code-health/repowise-health.json"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def collect_top_files(limit: int = 20) -> list[dict[str, Any]]:
    rows = [
        {"path": rel, **_file_metrics(p)}
        for p in _source_files()
        if "/tests/" not in f"/{(rel := _relative(p))}" and not rel.startswith("tests/")
    ]
    rows.sort(key=lambda item: (int(item["tokens"]), int(item["loc"])), reverse=True)
    return rows[:limit]


def collect_top_functions(root: Path = ROOT, limit: int = 20) -> list[dict[str, Any]]:
    exe = shutil.which("lizard")
    if exe is None:
        return []
    with tempfile.TemporaryDirectory(prefix="msl-digest-lizard-") as tmp:
        csv_path = Path(tmp) / "functions.csv"
        cmd = [
            exe,
            "-l",
            "python",
            "-l",
            "c",
            "-l",
            "javascript",
            "-x",
            "*/tests/*",
            "-x",
            "*/vendor/*",
            "-x",
            "*/__pycache__/*",
            "--csv",
            "-o",
            str(csv_path),
            "packages",
            "src",
        ]
        res = subprocess.run(cmd, cwd=root, check=False, capture_output=True, text=True)
        if res.returncode != 0 or not csv_path.is_file():
            return []
        with csv_path.open(encoding="utf-8", newline="") as handle:
            raw_rows = list(csv.reader(handle))
    items = [
        {
            "path": _relative(root / r[6]),
            "function": r[7],
            "nloc": int(r[0]),
            "ccn": int(r[1]),
            "tokens": int(r[2]),
            "parameters": int(r[3]),
        }
        for r in raw_rows
        if len(r) >= 11
    ]
    items.sort(key=lambda item: (int(item["tokens"]), int(item["ccn"])), reverse=True)
    return items[:limit]


def _query_git_churn(db_path: Path) -> dict[str, dict[str, Any]]:
    if not db_path.is_file():
        return {}
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT file_path, commit_count_90d, lines_added_90d + lines_deleted_90d, "
            "co_change_partners_json FROM git_metadata WHERE history_only = 0"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, dict[str, Any]] = {}
    for file_path, commits_90d, churn_90d, partners_raw in rows:
        partners = json.loads(partners_raw or "[]")
        out[str(file_path)] = {
            "commits_90d": int(commits_90d or 0),
            "churn_90d": int(churn_90d or 0),
            "partners": partners if isinstance(partners, list) else [],
        }
    return out


def collect_hotspots(
    repowise_health: dict[str, Any], git_meta: dict[str, dict[str, Any]], limit: int = 10
) -> list[dict[str, Any]]:
    metrics = repowise_health.get("metrics")
    if not isinstance(metrics, list):
        return []
    hotspots: list[dict[str, Any]] = []
    for entry in metrics:
        path = str(entry.get("file_path") or "") if isinstance(entry, dict) else ""
        if not path or "/tests/" in f"/{path}" or path.startswith("docs/"):
            continue
        score = float(entry.get("score") or 10.0)
        meta = git_meta.get(path, {})
        commits = int(meta.get("commits_90d", 0))
        hotspots.append(
            {
                "path": path,
                "score": round(score, 2),
                "nloc": int(entry.get("nloc") or 0),
                "max_ccn": int(entry.get("max_ccn") or 0),
                "commits_90d": commits,
                "churn_90d": int(meta.get("churn_90d", 0)),
                "priority": round((10.0 - score) * (1.0 + commits), 2),
            }
        )
    hotspots.sort(key=lambda item: (float(item["priority"]), -float(item["score"])), reverse=True)
    return hotspots[:limit]


def _is_code_path(path: str, root: Path) -> bool:
    if not path or path.startswith(("docs/", "README", ".")):
        return False
    if path.endswith((".md", ".lock", ".toml")):
        return False
    return (root / path).is_file()


def _partner_record(
    src: str, partner: Any, root: Path
) -> tuple[tuple[str, str], dict[str, Any]] | None:
    if not isinstance(partner, dict):
        return None
    dst = str(partner.get("file_path") or "")
    if dst == src or not _is_code_path(dst, root):
        return None
    key: tuple[str, str] = (src, dst) if src < dst else (dst, src)
    return key, {
        "left": key[0],
        "right": key[1],
        "frequency": int(partner.get("frequency") or 0),
        "weight": round(float(partner.get("co_change_count") or 0.0), 3),
        "structural": str(partner.get("structural") or "unknown"),
    }


def collect_coupled_pairs(
    git_meta: dict[str, dict[str, Any]], root: Path = ROOT, limit: int = 10
) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for src, meta in git_meta.items():
        if not _is_code_path(src, root):
            continue
        for partner in meta.get("partners", []):
            parsed = _partner_record(src, partner, root)
            if parsed is None:
                continue
            pair, rec = parsed
            if pair not in seen or float(rec["weight"]) > float(seen[pair]["weight"]):
                seen[pair] = rec
    return sorted(
        seen.values(),
        key=lambda item: (float(item["weight"]), int(item["frequency"])),
        reverse=True,
    )[:limit]


def summarize_jscpd(report: dict[str, Any]) -> dict[str, Any]:
    total = report.get("statistics", {}).get("total", {})
    raw_dups = [d for d in report.get("duplicates", []) if isinstance(d, dict)]
    raw_dups.sort(key=lambda d: int(d.get("lines") or 0), reverse=True)
    top_clones = [
        {
            "format": str(d.get("format") or ""),
            "lines": int(d.get("lines") or 0),
            "first": f"{d.get('firstFile', {}).get('name')}:{d.get('firstFile', {}).get('start')}-{d.get('firstFile', {}).get('end')}",
            "second": f"{d.get('secondFile', {}).get('name')}:{d.get('secondFile', {}).get('start')}-{d.get('secondFile', {}).get('end')}",
        }
        for d in raw_dups[:10]
    ]
    return {
        "sources": int(total.get("sources") or 0),
        "lines": int(total.get("lines") or 0),
        "clones": int(total.get("clones") or 0),
        "duplicated_lines": int(total.get("duplicatedLines") or 0),
        "percentage_lines": float(total.get("percentage") or 0.0),
        "percentage_tokens": float(total.get("percentageTokens") or 0.0),
        "top_clones": top_clones,
    }


def build_digest_payload(
    *,
    jscpd_path: Path = DEFAULT_JSCPD_REPORT,
    repowise_health_path: Path = DEFAULT_REPOWISE_HEALTH,
    repowise_db_path: Path = DEFAULT_REPOWISE_DB,
    root: Path = ROOT,
) -> dict[str, Any]:
    repowise_health = _load_json(repowise_health_path)
    git_meta = _query_git_churn(repowise_db_path)
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kpis": repowise_health.get("kpis", {}),
        "hotspots": collect_hotspots(repowise_health, git_meta, limit=10),
        "coupled_pairs": collect_coupled_pairs(git_meta, root=root, limit=10),
        "duplication": summarize_jscpd(_load_json(jscpd_path)),
        "top_files": collect_top_files(limit=20),
        "top_functions": collect_top_functions(root=root, limit=20),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jscpd-json", type=Path, default=DEFAULT_JSCPD_REPORT)
    parser.add_argument("--repowise-health", type=Path, default=DEFAULT_REPOWISE_HEALTH)
    parser.add_argument("--repowise-db", type=Path, default=DEFAULT_REPOWISE_DB)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON_OUT)
    args = parser.parse_args(argv)

    payload = build_digest_payload(
        jscpd_path=args.jscpd_json,
        repowise_health_path=args.repowise_health,
        repowise_db_path=args.repowise_db,
    )
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(render_digest_markdown(payload), encoding="utf-8")
    args.output_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
