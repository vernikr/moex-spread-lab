"""A registered daily pair plugin enters proof and Optuna with validated accounting."""

import os
import subprocess
import sys
from pathlib import Path

from p72_packages_proof_tests_test_pair_plugin_integration import PLUGIN_SOURCE, SCRIPT


def test_drop_in_pair_plugin_proof_and_search(tmp_path):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "demo_pair.py").write_text(PLUGIN_SOURCE)
    dist = plugin_dir / "demo_pair-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: demo-pair\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text(
        "[moex_spread_lab.strategies]\ndemo_pair = demo_pair:PLUGIN\ndemo_pair_hourly = demo_pair:HOURLY\n"
    )
    root = Path(__file__).resolve().parents[3]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(plugin_dir),
            str(root / "tests"),
            str(root / "packages/proof/tests"),
            str(root / "packages/accounting/tests"),
            env.get("PYTHONPATH", ""),
        ]
    )
    env["MOEX_PROOF_WORKERS"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(tmp_path / "root")],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
