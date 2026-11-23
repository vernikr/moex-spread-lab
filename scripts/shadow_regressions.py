#!/usr/bin/env python3
"""Known-regression injections for S3 shadow verification (plan S3, work item 3).

Each injection applies a deterministic break to a worktree that the full suite
MUST catch. Categories: registry discovery, proof parity, fixture change, JS break.
Injections are revertible with git reset --hard + clean. Returns a description.
"""

from __future__ import annotations

import json as _json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class RegressionInjection:
    slug: str
    category: str  # "registry" | "parity" | "fixture" | "js"
    description: str
    apply: Callable[[Path], str]


def _find_first(dir_path: Path, glob: str) -> Path | None:
    if not dir_path.is_dir():
        return None
    for candidate in sorted(dir_path.rglob(glob)):
        return candidate
    return None


def _inject_registry_break(root: Path) -> str:
    """Break plugin/strategy registry discovery (import or contract attr)."""
    strat = root / "packages/strategies/src/moex_spread_lab/strategies"
    if strat.is_dir():
        for sub in sorted(strat.iterdir()):
            init = sub / "__init__.py"
            if init.is_file():
                text = init.read_text(encoding="utf-8")
                init.write_text(
                    text.replace("from .engine import", "from .engin import  # shadow break"),
                    encoding="utf-8",
                )
                return f"broke import in {sub.name}/__init__.py"
    contracts = root / "packages/core/src/moex_spread_lab/core/contracts.py"
    text = contracts.read_text(encoding="utf-8")
    broken = text.replace(
        'STRATEGY_DISCOVERY_ATTR = "engine"', 'STRATEGY_DISCOVERY_ATTR = "engin"  # shadow break'
    )
    if broken == text:
        broken = text + "\n# shadow-registry-injection\n"
    contracts.write_text(broken, encoding="utf-8")
    return "broke STRATEGY_DISCOVERY_ATTR in core/contracts.py"


def _inject_parity_break(root: Path) -> str:
    """Break a numerical/parity invariant (rounding or hashing helper)."""
    acct = root / "packages/accounting/src/moex_spread_lab/accounting"
    for candidate in list(acct.rglob("*.py")) if acct.is_dir() else []:
        text = candidate.read_text(encoding="utf-8")
        if "_round" in text or "round(" in text:
            broken = re.sub(r"def (_round\w*)\s*\(", r"def \1_shadow_break(", text, count=1)
            if broken != text:
                candidate.write_text(broken, encoding="utf-8")
                return f"renamed rounding helper in {candidate.relative_to(root)}"
    core = root / "packages/core/src/moex_spread_lab/core/hashed_json_cache.py"
    if core.is_file():
        text = core.read_text(encoding="utf-8")
        core.write_text(
            text.replace("def canonical_json(", "def canonical_json_shadow_break(", 1),
            encoding="utf-8",
        )
        return "renamed canonical_json in core/hashed_json_cache.py"
    return "no parity-sensitive file found; skipped"


def _mutate_json(data):
    """Mutate a parsed JSON value slightly; returns True if a numeric field changed."""
    if isinstance(data, dict):
        for key in ("risk_free_rate", "start_capital", "lot_size"):
            if key in data and isinstance(data[key], (int, float)):
                data[key] = data[key] + 0.0001
                return True
    return False


def _inject_fixture_change(root: Path) -> str:
    """Mutate a non-.py JSON fixture that tests depend on."""
    fixture_dir = root / "fixtures/research"
    target = fixture_dir / "offline.json"
    if not target.is_file():
        target = _find_first(fixture_dir, "*.json")
    if target is None or not target.is_file():
        return "no JSON fixture found; skipped"
    try:
        data = _json.loads(target.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        text = target.read_text(encoding="utf-8", errors="replace")
        target.write_text(text + "\n// shadow-fixture-injection\n", encoding="utf-8")
        return f"appended marker to {target.relative_to(root)}"
    if not _mutate_json(data):
        data["_shadow_injection"] = True
    target.write_text(_json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return f"mutated fixture value in {target.relative_to(root)}"


def _inject_js_break(root: Path) -> str:
    """Introduce a static JS syntax error."""
    js_dir = root / "packages/web/src/moex_spread_lab/web/static/js"
    candidate = _find_first(js_dir, "*.js")
    if candidate is None:
        candidate = _find_first(root / "packages/web", "*.js")
    if candidate is None:
        return "no .js file found; skipped"
    text = candidate.read_text(encoding="utf-8", errors="replace")
    candidate.write_text(text + "\nconst SHADOW_BREAK =;\n", encoding="utf-8")
    return f"introduced JS syntax error in {candidate.relative_to(root)}"


REGRESSIONS: tuple[RegressionInjection, ...] = (
    RegressionInjection(
        "registry-discovery",
        "registry",
        "Break strategy/plugin registry discovery",
        _inject_registry_break,
    ),
    RegressionInjection(
        "proof-parity", "parity", "Break a numerical/parity invariant", _inject_parity_break
    ),
    RegressionInjection(
        "fixture-change", "fixture", "Mutate a non-.py JSON fixture", _inject_fixture_change
    ),
    RegressionInjection("js-module", "js", "Introduce a static JS syntax error", _inject_js_break),
)


def get_injection(slug: str) -> RegressionInjection:
    for inj in REGRESSIONS:
        if inj.slug == slug:
            return inj
    available = ", ".join(i.slug for i in REGRESSIONS)
    raise KeyError(f"unknown regression '{slug}'; available: {available}")


def apply_injection(root: Path, slug: str) -> str:
    """Apply named injection; return description of what broke."""
    return get_injection(slug).apply(root)


def reset_worktree(root: Path) -> None:
    """Discard all changes (git reset --hard + clean)."""
    subprocess.run(
        ["git", "reset", "--hard", "-q"], cwd=root, check=False, capture_output=True, timeout=120
    )
    subprocess.run(
        ["git", "clean", "-fd", "-q"], cwd=root, check=False, capture_output=True, timeout=120
    )


def list_injections() -> list[tuple[str, str, str]]:
    return [(i.slug, i.category, i.description) for i in REGRESSIONS]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", nargs="?", help="injection slug")
    parser.add_argument("--list", action="store_true", help="list injections")
    parser.add_argument("--reset", action="store_true", help="reset worktree")
    args = parser.parse_args()
    if args.list:
        for slug, cat, desc in list_injections():
            print(f"{slug:20s} [{cat:10s}] {desc}")
        raise SystemExit(0)
    if args.reset:
        reset_worktree(ROOT)
        print("worktree reset")
        raise SystemExit(0)
    if args.slug:
        print(f"applied '{args.slug}': {apply_injection(ROOT, args.slug)}")
        raise SystemExit(0)
    parser.print_help()
