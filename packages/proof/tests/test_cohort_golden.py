"""Full proof-search snapshot captured before the cohort-preparation decomposition.

Synthetic inputs only. The digest covers the complete canonical payload after replacing
only the fixture-root path; it is not a claim about market data or executable returns.
"""

import hashlib
import json
from unittest.mock import patch

from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.proof_runtime.proof_search import research_run
from moex_spread_lab.proof_runtime.service import ProofService
from proof_fixtures import fixture


def test_mixed_cohort_full_payload_and_run_id_before_decomposition(tmp_path):
    fixture(tmp_path)
    save_settings(tmp_path, {"random_seed": 41, "validation_percent": 25})
    request = {
        "universe": "pair_screen",
        "pair": "CNYRUBF/CRZ6",
        "strategy": "mean_reversion",
        "parameters": {"lookback": 10, "entry_z": 1.5, "exit_z": 0.5},
        "targets": [
            {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6"},
            {"universe": "calendar_native", "asset": "SR", "pair": "SRH3/SRM3"},
        ],
    }
    events = []
    with patch.dict("os.environ", {"MOEX_PROOF_WORKERS": "1", "MOEX_OFFLINE_ONLY": "1"}):
        service = ProofService(tmp_path)
        try:
            result = research_run(service, request, optimize=True, on_progress=events.append)
        finally:
            service.close()
    canonical = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    canonical = canonical.replace(str(tmp_path), "$ROOT")
    assert result["run_id"] == "bcbe159188c1eb07"
    assert hashlib.sha256(canonical.encode()).hexdigest() == (
        "d843de317632632f7bb9370223dbf641dc2d64f4a58405249ee81994426177c0"
    )
    assert {event["stage"] for event in events} >= {"prepare", "search", "report", "plateau"}
