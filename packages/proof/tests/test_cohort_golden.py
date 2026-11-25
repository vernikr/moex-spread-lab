"""Full proof-search snapshot captured before the cohort-preparation decomposition.

Synthetic inputs only. It is not a claim about market data or executable returns.

The payload is pinned twice, on purpose:

* **byte-exact**, on the platform the golden was recorded on — the strictest guard;
* **numerically**, everywhere — every float rounded to 9 decimals and every generated
  ``*run_id`` replaced by a placeholder before hashing.

The second form exists because float arithmetic is not bit-reproducible across platforms:
macOS and Linux disagree in the last bits of a handful of aggregates (``2.9e-17``-scale),
which is enough to move a byte-exact digest and every ``run_id`` derived from those
numbers. The rounded form agrees byte for byte on both, so a real regression still fails
everywhere while the platform's last-bit noise does not.
"""

import hashlib
import json
import platform
from unittest.mock import patch

import pytest

from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.service import ProofService
from proof_fixtures import fixture

# Recorded on CPython 3.12 / Linux (CI). Byte-exact only there; see the module docstring.
BYTE_EXACT_PLATFORM = "Linux"
BYTE_EXACT_RUN_ID = "bcbe159188c1eb07"
BYTE_EXACT_DIGEST = "d843de317632632f7bb9370223dbf641dc2d64f4a58405249ee81994426177c0"
# Platform-independent: floats rounded to 9 decimals, generated ids replaced.
NUMERIC_DIGEST = "3d9099e0c7d4b7a35f28b12153085f455be6ecae32b76581e42993ae8dd5ed9c"
ROUND_DIGITS = 9


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _numeric_form(value):
    """Rounded floats and placeholdered ids: the part of the payload every platform shares."""
    if isinstance(value, dict):
        return {
            key: (
                "<run-id>"
                if key.endswith("run_id") and isinstance(item, str)
                else _numeric_form(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_numeric_form(item) for item in value]
    if isinstance(value, float):
        return round(value, ROUND_DIGITS)
    return value


def _run_mixed_cohort(tmp_path) -> tuple[dict, list[dict]]:
    """Run the pinned mixed-cohort search on the synthetic fixture; return its events too."""
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
    events: list[dict] = []
    with patch.dict("os.environ", {"MOEX_PROOF_WORKERS": "1", "MOEX_OFFLINE_ONLY": "1"}):
        service = ProofService(tmp_path)
        try:
            result = research_run(service, request, optimize=True, on_progress=events.append)
        finally:
            service.close()
    return result, events


def test_mixed_cohort_payload_is_unchanged_on_every_platform(tmp_path):
    """The regression guard: same numbers, same stages, wherever the suite runs."""
    result, events = _run_mixed_cohort(tmp_path)
    numeric = _canonical(_numeric_form(result)).replace(str(tmp_path), "$ROOT")
    assert hashlib.sha256(numeric.encode()).hexdigest() == NUMERIC_DIGEST, (
        "the search result changed; re-record NUMERIC_DIGEST only with a justified plan deviation"
    )
    assert {event["stage"] for event in events} >= {"prepare", "search", "report", "plateau"}


@pytest.mark.skipif(
    platform.system() != BYTE_EXACT_PLATFORM,
    reason=(
        f"byte-exact golden recorded on {BYTE_EXACT_PLATFORM}; "
        "the numeric golden covers this platform"
    ),
)
def test_mixed_cohort_full_payload_and_run_id_before_decomposition(tmp_path):
    """The strictest guard, on the platform its golden was recorded on."""
    result, _ = _run_mixed_cohort(tmp_path)
    canonical = _canonical(result).replace(str(tmp_path), "$ROOT")
    assert result["run_id"] == BYTE_EXACT_RUN_ID
    assert hashlib.sha256(canonical.encode()).hexdigest() == BYTE_EXACT_DIGEST
