"""A third-party calendar plugin can enter the real proof and cohort search."""

import os
import subprocess
import sys
from pathlib import Path


def test_drop_in_calendar_plugin_proof_and_search(tmp_path):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "demo_strategy.py").write_text("""
from moex_spread_lab.strategies.calendar_zscore.plugin import CalendarZscorePlugin

class Demo(CalendarZscorePlugin):
    strategy_id = "demo_calendar"
    family_id = "demo_calendar"
    proof_adapter = "calendar_price_intents.v1"

PLUGIN = Demo()
""")
    dist = plugin_dir / "demo_strategy-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: demo-strategy\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text(
        "[moex_spread_lab.strategies]\ndemo_calendar = demo_strategy:PLUGIN\n"
    )
    root = Path(__file__).resolve().parents[3]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(plugin_dir),
            str(root / "tests"),
            str(root / "packages/accounting/tests"),
            str(root / "packages/proof/tests"),
            str(root / "packages/strategies/tests/contract"),
            env.get("PYTHONPATH", ""),
        ]
    )
    env["MOEX_PROOF_WORKERS"] = "1"
    code = """
import sys
from pathlib import Path
from proof_fixtures import two_calendar_windows
from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.proof.service import ProofService
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.proof_kernel import STRATEGY_AXES
root = Path(sys.argv[1])
two_calendar_windows(root)
save_settings(root, {"random_seed": 41})
assert "demo_calendar" in STRATEGY_AXES
from test_strategy_conformance import _calendar_fixture
from moex_spread_lab.strategies import discover_strategies
from moex_spread_lab.strategies.conformance import assert_causal_conformance
fixture, chart = _calendar_fixture()
assert_causal_conformance(discover_strategies().get("demo_calendar"), fixture, chart,
                          {"window": 6, "entry_z": 1.5, "exit_z": 0.5}, cutoffs=(14, 20, 26))
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

# Both H1 price sources, all fills, and holdout accounting match the legacy engine.
import json
from test_perf_equivalence import _build_h1_workspace, _write_h1_candles
h1_root = root.parent / "h1_root"
_build_h1_workspace(h1_root)
save_settings(h1_root, {"random_seed": 41, "timeframe": "1h", "validation_percent": 20,
                        "allow_failed_volume": True, "allow_failed_capital": True,
                        "initial_capital_rub": 100000})
for mode in ("synthetic_leg_proxy", "native_h1_spread"):
    if mode == "native_h1_spread":
        near = json.loads((h1_root / "data/w1/candles/SRH3_1h.json").read_text())["rows"]
        far = {r["begin"]: r for r in json.loads((h1_root / "data/w1/candles/SRM3_1h.json").read_text())["rows"]}
        spread = [{"begin": n["begin"], "open": far[n["begin"]]["open"] - n["open"],
                   "close": far[n["begin"]]["close"] - n["close"], "volume": 40}
                  for n in near if n["begin"] in far]
        _write_h1_candles(h1_root, "candles", "SRH3SRM3", spread)
    h1_service = ProofService(h1_root)
    for fill in ("next_open", "next_close", "reference_same_bar"):
        h1_request = {**request, "fill": fill,
                      "parameters": {"window": 10, "entry_z": 1.2, "exit_z": 0.3}}
        actual = research_run(h1_service, h1_request, optimize=False, include_plateau=False)
        reference = research_run(h1_service, {**h1_request, "strategy": "zscore_mean_reversion"},
                                 optimize=False, include_plateau=False)
        assert actual["window"]["price_source"] == mode
        assert actual["trades"] == reference["trades"], (mode, fill)
        assert actual["ledger"] == reference["ledger"], (mode, fill)
        assert actual["summary"] == reference["summary"], (mode, fill)

"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "data_root")],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    conformance = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(root / "packages/strategies/tests/contract/test_strategy_conformance.py"),
            "-q",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert conformance.returncode == 0, conformance.stdout + conformance.stderr
