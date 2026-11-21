"""A third-party calendar plugin can enter the real proof and cohort search."""

import os
from pathlib import Path
import subprocess
import sys


def test_drop_in_calendar_plugin_proof_and_search(tmp_path):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "demo_strategy.py").write_text('''
from moex_spread_lab.strategies.calendar_zscore.plugin import CalendarZscorePlugin

class Demo(CalendarZscorePlugin):
    strategy_id = "demo_calendar"
    family_id = "demo_calendar"
    proof_adapter = "calendar_price_intents.v1"

PLUGIN = Demo()
''')
    dist = plugin_dir / "demo_strategy-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: demo-strategy\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text(
        "[moex_spread_lab.strategies]\ndemo_calendar = demo_strategy:PLUGIN\n"
    )
    root = Path(__file__).resolve().parents[3]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([
        str(plugin_dir), str(root / "tests"), str(root / "packages/accounting/tests"),
        str(root / "packages/proof/tests"), env.get("PYTHONPATH", ""),
    ])
    env["MOEX_PROOF_WORKERS"] = "1"
    code = '''
import sys
from pathlib import Path
from proof_fixtures import two_calendar_windows
from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.proof_runtime.service import ProofService
from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.proof_runtime.proof_kernel import STRATEGY_AXES
root = Path(sys.argv[1])
two_calendar_windows(root)
save_settings(root, {"random_seed": 41})
assert "demo_calendar" in STRATEGY_AXES
request = {"universe": "calendar_native", "asset": "SR", "pair": "SRH3/SRM3",
           "strategy": "demo_calendar", "parameters": {"window": 10, "entry_z": 1.5, "exit_z": 0.5},
           "sizing": "max_affordable", "fill": "next_close"}
service = ProofService(root)
proof = research_run(service, request, optimize=False, include_plateau=False)
assert proof["selection"]["strategy"] == "demo_calendar"
assert set(proof["selection"]["parameters"]) == {"window", "entry_z", "exit_z"}
assert proof["summary"]["ledger_reconciliation_rub"] == 0
reference = research_run(service, {**request, "strategy": "zscore_mean_reversion"}, optimize=False, include_plateau=False)
assert proof["trades"] == reference["trades"]
assert proof["ledger"] == reference["ledger"]
assert proof["summary"] == reference["summary"]
result = research_run(service, {**request, "optimize_strategy": True}, optimize=True)
alternatives = result["strategy_search"]["alternatives"]
assert [a["strategy"] for a in alternatives] == ["zscore_mean_reversion", "convergence_to_ruonia", "demo_calendar"]
assert next(a for a in alternatives if a["strategy"] == "demo_calendar")["trial_count"] == 80
'''
    process = subprocess.run([sys.executable, "-c", code, str(tmp_path / "data_root")],
                             env=env, capture_output=True, text=True, timeout=180)
    assert process.returncode == 0, process.stdout + process.stderr
