"""Markdown formatter for the weekly Tier 3 code-health & behavioral digest."""

from __future__ import annotations

from typing import Any

_NUMERIC_COLS = {
    "#",
    "Health",
    "NLOC",
    "Max CCN",
    "90d Commits",
    "90d Churn",
    "Priority",
    "Co-changes",
    "Score",
    "Lines",
    "LOC",
    "Tokens",
    "CCN",
    "Params",
}


def _md_table(title: str, headers: list[str], rows: list[list[str]], empty_msg: str) -> list[str]:
    sep = ["---:" if col in _NUMERIC_COLS else "---" for col in headers]
    out = [f"## {title}", "", "| " + " | ".join(headers) + " |", "| " + " | ".join(sep) + " |"]
    if not rows:
        out.append("| – | " + f"_{empty_msg}_ | " + " | ".join("–" for _ in headers[2:]) + " |")
        return out
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    return out


def render_digest_markdown(payload: dict[str, Any]) -> str:
    """Render the structured Tier 3 digest payload as a GitHub Step Summary Markdown report."""
    kpis = payload.get("kpis", {})
    avg_h = kpis.get("production_average") or kpis.get("average_health")
    dup = payload["duplication"]
    lines = [
        "# Weekly Code-Health & Behavioral Digest",
        "",
        f"- **Generated (UTC):** `{payload['generated_at']}`",
        f"- **Repowise production health:** avg `{avg_h}/10`, hotspot `{kpis.get('hotspot_health')}/10`"
        if avg_h is not None
        else "- **Repowise production health:** _not provided_",
        f"- **Duplication (`jscpd`):** `{dup['duplicated_lines']}` / `{dup['lines']}` lines "
        f"(`{dup['percentage_lines']:.2f}%` lines, `{dup['percentage_tokens']:.2f}%` tokens "
        f"across `{dup['sources']}` files; `< 3.00%` budget)",
        "",
        *_md_table(
            "Top-10 Hotspots (Lowest Code Health × 90d Churn)",
            ["#", "File", "Health", "NLOC", "Max CCN", "90d Commits", "90d Churn", "Priority"],
            [
                [
                    str(i),
                    f"`{h['path']}`",
                    f"{h['score']:.2f}",
                    str(h["nloc"]),
                    str(h["max_ccn"]),
                    str(h["commits_90d"]),
                    str(h["churn_90d"]),
                    f"{h['priority']:.2f}",
                ]
                for i, h in enumerate(payload["hotspots"], 1)
            ],
            "No repowise health metrics available",
        ),
        "",
        *_md_table(
            "Top Change-Coupled File Pairs",
            ["#", "File A", "File B", "Co-changes", "Score", "Link"],
            [
                [
                    str(i),
                    f"`{p['left']}`",
                    f"`{p['right']}`",
                    str(p["frequency"]),
                    f"{p['weight']:.3f}",
                    f"`{p['structural']}`",
                ]
                for i, p in enumerate(payload["coupled_pairs"], 1)
            ],
            "No active co-change pairs recorded",
        ),
        "",
        *_md_table(
            "Top Duplicated Code Clones (`jscpd`)",
            ["#", "Format", "Lines", "First Location", "Second Location"],
            [
                [str(i), f"`{c['format']}`", str(c["lines"]), f"`{c['first']}`", f"`{c['second']}`"]
                for i, c in enumerate(dup.get("top_clones", []), 1)
            ],
            "No clones detected",
        ),
        "",
        *_md_table(
            "Top-20 Token-Heaviest Production Files",
            ["#", "File", "LOC", "Tokens"],
            [
                [str(i), f"`{f['path']}`", str(f["loc"]), str(f["tokens"])]
                for i, f in enumerate(payload["top_files"], 1)
            ],
            "None",
        ),
        "",
        *_md_table(
            "Top-20 Token-Heaviest Production Functions (`lizard`)",
            ["#", "Function", "NLOC", "CCN", "Tokens", "Params"],
            [
                [
                    str(i),
                    f"`{fn['path']}::{fn['function']}`",
                    str(fn["nloc"]),
                    str(fn["ccn"]),
                    str(fn["tokens"]),
                    str(fn["parameters"]),
                ]
                for i, fn in enumerate(payload["top_functions"], 1)
            ],
            "None",
        ),
        "",
    ]
    return "\n".join(lines)
