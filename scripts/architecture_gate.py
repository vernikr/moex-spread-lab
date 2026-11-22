"""Run the repository-wide, dependency-light architecture gate.

This script intentionally reuses the checked-in DAG/conformance tests and invokes
both governance CLI commands in-process. It is designed for the fast CI gate;
full behavioral and numerical suites run in the package test matrix.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

# Make the script usable both from an installed uv workspace and from a plain
# repository checkout, without adding a runtime dependency or build step.
for source in sorted((ROOT / "packages").glob("*/src"), reverse=True):
    sys.path.insert(0, str(source))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "packages" / "core" / "tests"))
sys.path.insert(0, str(ROOT / "packages" / "strategies" / "tests" / "contract"))

from test_strategy_conformance import StrategyPluginConformanceTests  # noqa: E402

from test_architecture_dag import ArchitectureDagTests  # noqa: E402


def main() -> int:
    started = time.perf_counter()
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(ArchitectureDagTests))
    suite.addTests(
        StrategyPluginConformanceTests(name)
        for name in (
            "test_auto_discovers_all_builtin_strategy_plugins",
            "test_synthetic_drop_in_strategy_folder_registers_without_core_edits",
        )
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        return 1

    from moex_spread_lab import cli

    commands = (
        ["validate-manifest", "--manifest", "config/run-manifest.json"],
        ["secret-scan", "--tracked-only"],
    )
    original_argv = sys.argv
    try:
        for args in commands:
            sys.argv = ["moex-spread-lab", *args]
            if cli.main() != 0:
                return 1
    finally:
        sys.argv = original_argv

    print(f"architecture-gate: passed in {time.perf_counter() - started:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
