"""Streaming-progress tier of the bit-for-bit equivalence suite (plan S4a).

Progress callbacks across every proof entrypoint (``research_run``, ``checked_plateau``,
``window_summaries``) must match the non-streaming payloads bit for bit, with monotonic
percentages and well-formed stage/label/count fields.
Split from the worker-pool tier so each file stays inside the health gate's
new-file budget; both carry ``pytest.mark.slow_equiv`` and run together in every
whole-suite path (``--pre-push``, CI perf-equivalence, sanitizer gate).
"""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.proof.proof_batch import checked_plateau
from moex_spread_lab.proof.proof_pool import get_worker_pool
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.proof_windows import window_summaries
from moex_spread_lab.proof.service import PAIR_UNIVERSE, ProofService
from perf_equivalence_fixtures import _canon
from perf_equivalence_pool_fixtures import _build_pool_test_workspace

pytestmark = pytest.mark.slow_equiv


class ProofPerfEquivalenceStreamingTests(unittest.TestCase):
    """Progress streaming: monotonic, contract-complete, identical to plain JSON."""

    def test_streaming_progress_is_monotonic_and_matches_json_bit_for_bit(self):
        """Every streamed endpoint must match its plain JSON body bit for bit."""
        assert_streaming_progress_matches_plain_json(self)


def _service_runner(service: ProofService):
    """Return ``(run_plain, run_stream)`` helpers backed directly by Layer-3 proof APIs."""

    def invoke(endpoint: str, body: dict, on_progress):
        payload = deepcopy(body)
        diagnostics = payload.pop("include_plateau", False) is True
        if endpoint == "/api/proof/run":
            return research_run(
                service,
                payload,
                optimize=False,
                include_plateau=diagnostics,
                on_progress=on_progress,
            )
        if endpoint == "/api/proof/optimize":
            return research_run(
                service,
                payload,
                optimize=True,
                on_progress=on_progress,
            )
        if endpoint == "/api/proof/plateau":
            return checked_plateau(service, payload, on_progress=on_progress)
        if endpoint == "/api/proof/windows":
            return window_summaries(service, payload, on_progress=on_progress)
        raise AssertionError(f"Unknown proof endpoint: {endpoint}")

    def run_plain(endpoint: str, body: dict):
        return invoke(endpoint, body, None)

    def run_stream(endpoint: str, body: dict):
        events: list[dict] = []
        result = invoke(endpoint, body, lambda ev: events.append({"type": "progress", **ev}))
        return events, result

    return run_plain, run_stream


def _assert_stream_contract(
    case, endpoint: str, plain: dict, events: list, streamed, min_events: int
):
    case.assertGreaterEqual(len(events), min_events, endpoint)
    pcts = [float(ev["percent"]) for ev in events]
    case.assertEqual(pcts, sorted(pcts), f"Non-monotonic progress on {endpoint}: {pcts}")
    case.assertGreaterEqual(pcts[0], 0.0)
    case.assertLessEqual(pcts[-1], 100.0)
    for ev in events:
        case.assertTrue(ev.get("stage"))
        case.assertTrue(ev.get("label"))
        case.assertGreaterEqual(ev["done"], 0)
        case.assertGreaterEqual(ev["total"], ev["done"])
    case.assertEqual(_canon(streamed), _canon(plain))


def assert_streaming_progress_matches_plain_json(case) -> None:
    """Every streamed entrypoint must match its non-streaming payload bit for bit."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_pool_test_workspace(root)
        service = ProofService(root, pool_factory=get_worker_pool)
        run_plain, run_stream = _service_runner(service)
        try:
            bodies = _stream_bodies()
            for endpoint, body, min_events in bodies:
                plain = run_plain(endpoint, body)
                events, streamed = run_stream(endpoint, body)
                _assert_stream_contract(case, endpoint, plain, events, streamed, min_events)
        finally:
            service.close()


def _stream_bodies() -> list[tuple[str, dict, int]]:
    targets = [
        {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRZ6"},
        {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRM6"},
        {"universe": PAIR_UNIVERSE, "pair": "CNYRUBF/CRU6"},
    ]
    parameters = {"lookback": 12, "entry_z": 1.5, "exit_z": 0.4}
    run_body = {
        "universe": PAIR_UNIVERSE,
        "pair": "CNYRUBF/CRZ6",
        "strategy": "mean_reversion",
        "fill": "next_open",
        "sizing": AUTO_SIZING,
        "parameters": parameters,
        "include_plateau": True,
    }
    opt_body = {
        "universe": PAIR_UNIVERSE,
        "pair": "CNYRUBF/CRZ6",
        "strategy": "mean_reversion",
        "fill": "next_open",
        "sizing": AUTO_SIZING,
        "optimize_strategy": True,
        "targets": targets,
    }
    plat_body = {
        "targets": targets,
        "strategy": "mean_reversion",
        "fill": "next_open",
        "sizing": AUTO_SIZING,
        "parameters": parameters,
    }
    win_body = {
        "requests": [
            {
                **target,
                "strategy": "mean_reversion",
                "fill": "next_open",
                "sizing": AUTO_SIZING,
                "parameters": parameters,
            }
            for target in targets
        ]
    }
    return [
        ("/api/proof/run", run_body, 3),
        ("/api/proof/optimize", opt_body, 10),
        ("/api/proof/plateau", plat_body, 2),
        ("/api/proof/windows", win_body, 2),
    ]


if __name__ == "__main__":
    unittest.main()
