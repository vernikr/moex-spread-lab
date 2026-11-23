"""Link-integrity gate for the authored documentation surface (Issue #141).

`scripts/docs_scan.py --repo --links` is the mechanical check behind the
"zero broken markdown links across README.md and docs/" acceptance criterion. It
resolves relative link targets and heading anchors across the authored docs
surface and skips preserved evidence, which is reproduced as received.
"""

from __future__ import annotations

from pathlib import Path

from scripts import docs_scan


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_broken_target_and_anchor_are_reported(tmp_path: Path) -> None:
    _write(tmp_path / "guide.md", "# Setup notes\n\n## Second part\n")
    page = _write(
        tmp_path / "docs" / "page.md",
        "[ok](../guide.md#second-part)\n"
        "[missing file](../absent.md)\n"
        "[missing anchor](../guide.md#absent)\n"
        "[external](https://example.com/x.md)\n",
    )

    findings, notes = docs_scan.scan_links([str(page)])

    text = "\n".join(findings)
    assert "unresolved link target ../absent.md" in text
    assert "unresolved link anchor ../guide.md#absent" in text
    assert "guide.md#second-part" not in text
    assert "example.com" not in text
    assert notes == []


def test_preserved_evidence_is_not_link_checked() -> None:
    findings, notes = docs_scan.scan_links(["docs/inputs/server-spec.redacted.md"])

    assert findings == []
    assert notes and "preserved as received" in notes[0]


def test_repo_surface_covers_authored_docs_and_excludes_preserved_evidence() -> None:
    paths = docs_scan.repo_doc_paths()

    assert "README.md" in paths
    assert "AGENTS.md" in paths
    assert "docs/development/github-project.md" in paths
    assert "packages/proof/docs/execution-proof.md" in paths
    assert "packages/core/docs/README.md" in paths
    assert "packages/web/docs/README.md" in paths
    assert not [path for path in paths if path.startswith(docs_scan._EXCLUDED_PREFIXES)]


def test_repository_documentation_has_no_broken_links() -> None:
    findings, _ = docs_scan.scan_links(docs_scan.repo_doc_paths())

    assert findings == []
