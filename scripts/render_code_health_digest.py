#!/usr/bin/env python3
"""Collect and render the weekly Tier 3 code-health, hotspot, coupling, and duplication digest."""

import argparse
import csv
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.code_health_gate_common import ROOT, _file_metrics, _relative, _source_files

REPORTS = ROOT / "reports/code-health"
DEFAULT_JSCPD_REPORT = REPORTS / "jscpd/jscpd-report.json"
DEFAULT_REPOWISE_DB = ROOT / ".repowise/wiki.db"
DEFAULT_REPOWISE_HEALTH = REPORTS / "repowise-health.json"
_TEXT_COLS = {
    "File",
    "File A",
    "File B",
    "Link",
    "Format",
    "First Location",
    "Second Location",
    "Function",
}
# (title, payload key, empty message, [(header, format_map template)]); "#" prepended.
_TABLES: tuple[tuple[str, str, str, list[tuple[str, str]]], ...] = (
    (
        "Top-10 Hotspots (Lowest Code Health × 90d Churn)",
        "hotspots",
        "No repowise health metrics available",
        [
            ("File", "`{path}`"),
            ("Health", "{score:.2f}"),
            ("NLOC", "{nloc}"),
            ("Max CCN", "{max_ccn}"),
            ("90d Commits", "{commits_90d}"),
            ("90d Churn", "{churn_90d}"),
            ("Priority", "{priority:.2f}"),
        ],
    ),
    (
        "Top Change-Coupled File Pairs",
        "coupled_pairs",
        "No active co-change pairs recorded",
        [
            ("File A", "`{left}`"),
            ("File B", "`{right}`"),
            ("Co-changes", "{frequency}"),
            ("Score", "{weight:.3f}"),
            ("Link", "`{structural}`"),
        ],
    ),
    (
        "Top Duplicated Code Clones (`jscpd`)",
        "top_clones",
        "No clones detected",
        [
            ("Format", "`{format}`"),
            ("Lines", "{lines}"),
            ("First Location", "`{first}`"),
            ("Second Location", "`{second}`"),
        ],
    ),
    (
        "Top-20 Token-Heaviest Production Files",
        "top_files",
        "None",
        [("File", "`{path}`"), ("LOC", "{loc}"), ("Tokens", "{tokens}")],
    ),
    (
        "Top-20 Token-Heaviest Production Functions (`lizard`)",
        "top_functions",
        "None",
        [
            ("Function", "`{path}::{function}`"),
            ("NLOC", "{nloc}"),
            ("CCN", "{ccn}"),
            ("Tokens", "{tokens}"),
            ("Params", "{parameters}"),
        ],
    ),
)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_test_or_doc(path: str) -> bool:
    return "/tests/" in f"/{path}" or path.startswith("docs/")


def collect_top_files(limit: int = 20) -> list[dict[str, Any]]:
    rows = [
        {"path": rel, **_file_metrics(p)}
        for p in _source_files()
        if not _is_test_or_doc(rel := _relative(p))
    ]
    return sorted(rows, key=lambda item: (item["tokens"], item["loc"]), reverse=True)[:limit]


def collect_top_functions(root: Path = ROOT, limit: int = 20) -> list[dict[str, Any]]:
    exe = shutil.which("lizard")
    if exe is None:
        return []
    excludes = [arg for part in ("tests", "vendor", "__pycache__") for arg in ("-x", f"*/{part}/*")]
    with tempfile.TemporaryDirectory(prefix="msl-digest-lizard-") as tmp:
        out = Path(tmp) / "functions.csv"
        langs = ["-l", "python", "-l", "c", "-l", "javascript"]
        cmd = [exe, *langs, *excludes, "--csv", "-o", str(out), "packages", "src"]
        if (
            subprocess.run(cmd, cwd=root, check=False, capture_output=True).returncode
            or not out.is_file()
        ):
            return []
        with out.open(encoding="utf-8", newline="") as handle:
            rows = [row for row in csv.reader(handle) if len(row) >= 11]
    keys = ("nloc", "ccn", "tokens", "parameters")
    items = [
        {
            "path": _relative(root / r[6]),
            "function": r[7],
            **{k: int(v) for k, v in zip(keys, r[:4], strict=True)},
        }
        for r in rows
    ]
    return sorted(items, key=lambda item: (item["tokens"], item["ccn"]), reverse=True)[:limit]


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
    for file_path, commits, churn, partners_raw in rows:
        partners = json.loads(partners_raw or "[]")
        out[str(file_path)] = {
            "commits_90d": int(commits or 0),
            "churn_90d": int(churn or 0),
            "partners": partners if isinstance(partners, list) else [],
        }
    return out


def collect_hotspots(
    health: dict[str, Any], git_meta: dict[str, dict[str, Any]], limit: int = 10
) -> list[dict]:
    metrics = health.get("metrics")
    hotspots: list[dict[str, Any]] = []
    for entry in metrics if isinstance(metrics, list) else []:
        path = str(entry.get("file_path") or "") if isinstance(entry, dict) else ""
        if not path or _is_test_or_doc(path):
            continue
        score, meta = float(entry.get("score") or 10.0), git_meta.get(path, {})
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
    return sorted(hotspots, key=lambda item: (item["priority"], -item["score"]), reverse=True)[
        :limit
    ]


def _is_code_path(path: str, root: Path) -> bool:
    excluded = path.startswith(("docs/", "README", ".")) or path.endswith((".md", ".lock", ".toml"))
    return bool(path) and not excluded and (root / path).is_file()


def _pair(src: str, partner: Any, root: Path) -> dict[str, Any] | None:
    dst = str(partner.get("file_path") or "") if isinstance(partner, dict) else ""
    if dst == src or not _is_code_path(dst, root):
        return None
    left, right = sorted((src, dst))
    return {
        "left": left,
        "right": right,
        "frequency": int(partner.get("frequency") or 0),
        "weight": round(float(partner.get("co_change_count") or 0.0), 3),
        "structural": str(partner.get("structural") or "unknown"),
    }


def collect_coupled_pairs(
    git_meta: dict[str, dict[str, Any]], root: Path = ROOT, limit: int = 10
) -> list[dict]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for src, meta in git_meta.items():
        partners = meta.get("partners", []) if _is_code_path(src, root) else []
        for rec in filter(None, (_pair(src, partner, root) for partner in partners)):
            key = (rec["left"], rec["right"])
            if key not in seen or rec["weight"] > seen[key]["weight"]:
                seen[key] = rec
    return sorted(
        seen.values(), key=lambda item: (item["weight"], item["frequency"]), reverse=True
    )[:limit]


def _location(side: Any) -> str:
    side = side if isinstance(side, dict) else {}
    return f"{side.get('name')}:{side.get('start')}-{side.get('end')}"


def summarize_jscpd(report: dict[str, Any]) -> dict[str, Any]:
    total = report.get("statistics", {}).get("total", {})
    dups = sorted(
        (d for d in report.get("duplicates", []) if isinstance(d, dict)),
        key=lambda d: int(d.get("lines") or 0),
        reverse=True,
    )
    return {
        "sources": int(total.get("sources") or 0),
        "lines": int(total.get("lines") or 0),
        "clones": int(total.get("clones") or 0),
        "duplicated_lines": int(total.get("duplicatedLines") or 0),
        "percentage_lines": float(total.get("percentage") or 0.0),
        "percentage_tokens": float(total.get("percentageTokens") or 0.0),
        "top_clones": [
            {
                "format": str(d.get("format") or ""),
                "lines": int(d.get("lines") or 0),
                "first": _location(d.get("firstFile")),
                "second": _location(d.get("secondFile")),
            }
            for d in dups[:10]
        ],
    }


def build_digest_payload(
    *,
    jscpd_path: Path = DEFAULT_JSCPD_REPORT,
    repowise_health_path: Path = DEFAULT_REPOWISE_HEALTH,
    repowise_db_path: Path = DEFAULT_REPOWISE_DB,
    root: Path = ROOT,
) -> dict[str, Any]:
    health, git_meta = _load_json(repowise_health_path), _query_git_churn(repowise_db_path)
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kpis": health.get("kpis", {}),
        "hotspots": collect_hotspots(health, git_meta),
        "coupled_pairs": collect_coupled_pairs(git_meta, root=root),
        "duplication": summarize_jscpd(_load_json(jscpd_path)),
        "top_files": collect_top_files(),
        "top_functions": collect_top_functions(root=root),
    }


def _md_table(
    title: str, rows: list[dict], empty: str, columns: list[tuple[str, str]]
) -> list[str]:
    headers = ["#", *(name for name, _ in columns)]
    sep = ["---" if col in _TEXT_COLS else "---:" for col in headers]
    out = [f"## {title}", "", f"| {' | '.join(headers)} |", f"| {' | '.join(sep)} |"]
    body = [
        [str(i), *(spec.format_map(row) for _, spec in columns)] for i, row in enumerate(rows, 1)
    ]
    out.extend(
        f"| {' | '.join(cells)} |"
        for cells in body or [["–", f"_{empty}_", *["–"] * (len(headers) - 2)]]
    )
    return [*out, ""]


def render_digest_markdown(payload: dict[str, Any]) -> str:
    kpis, dup = payload.get("kpis", {}), payload["duplication"]
    avg = kpis.get("production_average") or kpis.get("average_health")
    health = (
        f"avg `{avg}/10`, hotspot `{kpis.get('hotspot_health')}/10`"
        if avg is not None
        else "_not provided_"
    )
    lines = [
        "# Weekly Code-Health & Behavioral Digest",
        "",
        f"- **Generated (UTC):** `{payload['generated_at']}`",
        f"- **Repowise production health:** {health}",
        f"- **Duplication (`jscpd`):** `{dup['duplicated_lines']}` / `{dup['lines']}` lines "
        f"(`{dup['percentage_lines']:.2f}%` lines, `{dup['percentage_tokens']:.2f}%` tokens "
        f"across `{dup['sources']}` files; `< 3.00%` budget)",
        "",
    ]
    for title, key, empty, columns in _TABLES:
        lines += _md_table(
            title, (dup if key == "top_clones" else payload).get(key, []), empty, columns
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jscpd-json", type=Path, default=DEFAULT_JSCPD_REPORT)
    parser.add_argument("--repowise-health", type=Path, default=DEFAULT_REPOWISE_HEALTH)
    parser.add_argument("--repowise-db", type=Path, default=DEFAULT_REPOWISE_DB)
    parser.add_argument("--output-md", type=Path, default=REPORTS / "code-health-digest.md")
    parser.add_argument("--output-json", type=Path, default=REPORTS / "code-health-digest.json")
    args = parser.parse_args(argv)
    payload = build_digest_payload(
        jscpd_path=args.jscpd_json,
        repowise_health_path=args.repowise_health,
        repowise_db_path=args.repowise_db,
    )
    for path, text in (
        (args.output_md, render_digest_markdown(payload)),
        (args.output_json, json.dumps(payload, indent=2) + "\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
