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
# Re-recorded with the shared shortfall tolerance (refs #273, #297): the daily cash walk
# no longer grows a sub-tolerance negative balance, so the last bits of this payload moved
# while the platform-independent NUMERIC_DIGEST below stayed identical.
# Re-recorded again for #272: evaluation metadata now carries effective_validation_percent,
# which is part of the hashed payload (NUMERIC_DIGEST moved with it).
# Re-recorded again for #318: ResearchSettings.metadata() now embeds the
# research-settings.v1 schema marker (self-describing run identities). Verified: with the
# marker stripped the pipeline reproduces the previous hashes exactly, so this move is
# caused solely by the schema key.
# Re-recorded again for #344: simulate_marked_ledger now closes same-bar terminal
# calendar trades on the entry bar instead of skipping exit costs; verified diff is
# isolated to trial 7 SRH3/SRM3 (-7.417343105 -> -7.650809905 pp).
BYTE_EXACT_RUN_ID = "eb7f4a44bc2e2d20"
BYTE_EXACT_DIGEST = "bc344d71b6f213ac9ba72960be84b3b2b35d1ea3d73ebfabee352f9317ff949e"
# Platform-independent: floats rounded to 9 decimals, generated ids replaced.
NUMERIC_DIGEST = "8e025ee6ea3f6ed31437042e00b9516895c5b94f6e880f21e576932d6a857257"
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
    # Preserve the original byte-exact legacy fixture for this historical characterization.
    # Freshness behavior is exercised separately by the ISS cache contract tests.
    for directory in ("carry-history-cache", "spread-history-cache"):
        for path in (tmp_path / "data" / directory).glob("*.json"):
            doc = json.loads(path.read_text())
            doc.pop("cached_at", None)
            path.write_text(json.dumps(doc))
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
    with (
        patch.dict("os.environ", {"MOEX_PROOF_WORKERS": "1", "MOEX_OFFLINE_ONLY": "1"}),
        patch("moex_spread_lab.data.iss.iss_http.cache_page_is_eligible", return_value=True),
    ):
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
