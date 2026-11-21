"""A registered daily pair plugin enters proof and Optuna with validated accounting."""
import os
from pathlib import Path
import subprocess
import sys


def test_drop_in_pair_plugin_proof_and_search(tmp_path):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "demo_pair.py").write_text('''
from dataclasses import replace
from moex_spread_lab.strategies.pair_mean_reversion.plugin import PairMeanReversionPlugin
class Demo(PairMeanReversionPlugin):
    strategy_id = "demo_pair"
    family_id = "demo_pair"
    proof_adapter = "daily_pair_price_intents.v1"
    requires_daily_bars = True
    def evaluate_intents(self, window, params, fill):
        run = super().evaluate_intents(window, params, fill)
        days = [row["date"] for row in window.rows]
        delay = 0 if fill == "reference_same_bar" else 1
        return replace(run, intents=tuple({**item, "signal": {"kind": "z",
            "date": days[days.index(item["entry_date"][:10]) - delay],
            "value": item["entry_signal_z"]}} for item in run.intents))
PLUGIN = Demo()
''')
    dist = plugin_dir / "demo_pair-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: demo-pair\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text("[moex_spread_lab.strategies]\ndemo_pair = demo_pair:PLUGIN\n")
    root = Path(__file__).resolve().parents[3]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(plugin_dir), str(root / "tests"), str(root / "packages/proof/tests"), str(root / "packages/accounting/tests"), env.get("PYTHONPATH", "")])
    env["MOEX_PROOF_WORKERS"] = "1"
    code = '''
import sys
from pathlib import Path
from funding_fixtures import PAIR, fixture, request
from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime.service import ProofService
from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.proof_runtime.proof_kernel import STRATEGY_AXES
root = Path(sys.argv[1])
fixture(root)
save_settings(root, {"random_seed": 41, "validation_percent": 20})
assert "demo_pair" in STRATEGY_AXES
service = ProofService(root)
for fill in ("next_open", "next_close", "reference_same_bar"):
    base = {**request(), "sizing": "max_affordable", "fill": fill}
    base.pop("quantity")
    candidate = {**base, "strategy": "demo_pair"}
    actual = research_run(service, candidate, optimize=False, include_plateau=False)
    expected = research_run(service, base, optimize=False, include_plateau=False)
    assert actual["selection"]["strategy"] == "demo_pair"
    assert actual["trades"] == expected["trades"], (fill, actual["trades"], expected["trades"])
    assert actual["ledger"] == expected["ledger"], (fill, next(((a,e) for a,e in zip(actual["ledger"],expected["ledger"]) if a != e), None))
    assert actual["summary"]["ledger_reconciliation_rub"] == 0
    assert actual["evaluation"]["result"] == expected["evaluation"]["result"]
try:
    service.prepare({**request(), "strategy": "demo_pair"})
except W0Error:
    pass
else:
    raise AssertionError("fixed quantity must fail closed")
try:
    service.prepare({**candidate, "quantity": 2})
except W0Error:
    pass
else:
    raise AssertionError("auto sizing must reject an explicit quantity")
from moex_spread_lab.core.settings import ResearchSettings
try:
    service.prepare(candidate, settings=ResearchSettings(timeframe="1h"))
except W0Error:
    pass
else:
    raise AssertionError("hourly plugin accounting must fail closed")
prepared = service.prepare(candidate, settings=ResearchSettings())
try:
    prepared({**candidate, "pair": "GLZ6/GLH7"})
except W0Error:
    pass
else:
    raise AssertionError("prepared replay must reject another pair")
search = research_run(service, {**candidate, "optimize_strategy": True}, optimize=True)
row = next(item for item in search["strategy_search"]["alternatives"] if item["strategy"] == "demo_pair")
assert row["trial_count"] == 80, row
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "root")],
                            env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
