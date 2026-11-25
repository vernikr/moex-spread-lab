"""A new strategy folder must not require editing the shared architecture test.

The test copies the workspace sources into a temporary tree, drops in a brand-new
strategy subfolder and runs the real ``test_architecture_dag.py`` against the copy.
Living copies keep the probe out of the installed packages, so parallel test files
never see a strategy that does not exist on the branch.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DAG_TEST = REPO_ROOT / "packages/core/tests/test_architecture_dag.py"
PROBE_DIR = "packages/strategies/src/moex_spread_lab/strategies/probe_demo"

PROBE_PLUGIN = '''"""Probe strategy: a new folder is the only artefact a drop-in strategy adds."""

from moex_spread_lab.core.contracts import ParameterSpec, StrategyPlugin
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE


class ProbeDemoPlugin(StrategyPlugin):
    strategy_id = "probe_demo"
    family_id = "probe_demo"
    label_ru = "Пробная стратегия"
    short_label_ru = "Проба"
    supported_universes = frozenset({CALENDAR_UNIVERSE})
    supported_fills = ("next_open", "next_close", "reference_same_bar")
    requires_daily_bars = False
    proof_adapter = "calendar_price_intents.v1"

    def parameter_schema(self, context=None):
        return (
            ParameterSpec(
                name="window",
                label_ru="Ширина окна, баров",
                kind="int",
                default=20,
                min_value=10,
                max_value=40,
                step=1,
            ),
        )


PLUGIN = ProbeDemoPlugin()
'''

CROSS_IMPORT_PLUGIN = PROBE_PLUGIN.replace(
    "from moex_spread_lab.core.universe import CALENDAR_UNIVERSE",
    "from moex_spread_lab.core.universe import CALENDAR_UNIVERSE\n"
    "from moex_spread_lab.strategies.funding_carry.plugin import FundingCarryPlugin",
)


def _ignore(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name == "__pycache__"}


def _copy_workspace_sources(destination: Path) -> None:
    shutil.copytree(REPO_ROOT / "src", destination / "src", ignore=_ignore)
    for package_src in sorted((REPO_ROOT / "packages").glob("*/src")):
        package_root = destination / "packages" / package_src.parent.name / "src"
        shutil.copytree(package_src, package_root, ignore=_ignore)
    target = destination / "packages/core/tests/test_architecture_dag.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DAG_TEST, target)
    shutil.copy2(
        REPO_ROOT / "packages/core/tests/architecture_layers.py",
        target.parent / "architecture_layers.py",
    )


def _write_probe(root: Path, plugin_source: str) -> None:
    probe = root / PROBE_DIR
    probe.mkdir(parents=True)
    (probe / "__init__.py").write_text("", encoding="utf-8")
    (probe / "engine.py").write_text('"""Probe engine."""\n\nWINDOW = 20\n', encoding="utf-8")
    (probe / "plugin.py").write_text(plugin_source, encoding="utf-8")


def _run_dag_test(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "packages/core/tests/test_architecture_dag.py")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_new_strategy_folder_passes_the_shared_dag_test_without_edits(tmp_path):
    _copy_workspace_sources(tmp_path)
    baseline = _run_dag_test(tmp_path)
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    _write_probe(tmp_path, PROBE_PLUGIN)
    result = _run_dag_test(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_dynamic_discovery_still_rejects_cross_strategy_imports(tmp_path):
    _copy_workspace_sources(tmp_path)
    _write_probe(tmp_path, CROSS_IMPORT_PLUGIN)
    result = _run_dag_test(tmp_path)
    assert result.returncode != 0
    assert "funding_carry.plugin" in result.stdout + result.stderr
