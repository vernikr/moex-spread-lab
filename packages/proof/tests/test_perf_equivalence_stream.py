"""Streaming-progress tier of the bit-for-bit equivalence suite (plan S4a).

The NDJSON progress stream over a loopback ``ThreadingHTTPServer`` must match the
plain JSON response bit for bit on every endpoint, with monotonic percentages.
Split from the worker-pool tier so each file stays inside the health gate's
new-file budget; both carry ``pytest.mark.slow_equiv`` and run together in every
whole-suite path (``--pre-push``, CI perf-equivalence, sanitizer gate).
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.proof_runtime.service import PAIR_UNIVERSE
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs
from moex_spread_lab.web.server import ScreenerStore, make_handler
from perf_equivalence_fixtures import _canon
from perf_equivalence_pool_fixtures import _build_pool_test_workspace

pytestmark = pytest.mark.slow_equiv


class ProofPerfEquivalenceStreamingTests(unittest.TestCase):
    """Progress streaming: monotonic, contract-complete, identical to plain JSON."""

    def test_streaming_progress_is_monotonic_and_matches_json_bit_for_bit(self):
        """Every streamed endpoint must match its plain JSON body bit for bit."""
        assert_streaming_progress_matches_plain_json(self)


def _stream_client(base_url: str):
    """Return ``(post_json, post_stream)`` helpers for the loopback test server."""

    def post_json(endpoint: str, body: dict):
        req = urllib.request.Request(
            f"{base_url}{endpoint}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post_stream(endpoint: str, body: dict):
        req = urllib.request.Request(
            f"{base_url}{endpoint}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Progress-Stream": "1"},
        )
        events, result = [], None
        with urllib.request.urlopen(req, timeout=30) as resp:
            assert "application/x-ndjson" in resp.headers.get("Content-Type", "")
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                msg = json.loads(line)
                if msg["type"] == "progress":
                    events.append(msg)
                elif msg["type"] == "result":
                    result = msg["data"]
                else:
                    raise AssertionError(f"Unexpected stream message: {msg}")
        return events, result

    return post_json, post_stream


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
    """Every streamed endpoint must match its plain JSON body bit for bit."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_pool_test_workspace(root)
        store = ScreenerStore(ScreenerInputs(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        post_json, post_stream = _stream_client(f"http://127.0.0.1:{server.server_port}")
        try:
            bodies = _stream_bodies()
            for endpoint, body, min_events in bodies:
                plain = post_json(endpoint, body)
                events, streamed = post_stream(endpoint, body)
                _assert_stream_contract(case, endpoint, plain, events, streamed, min_events)
            _assert_static_ui_contract(case)
        finally:
            server.shutdown()
            server.server_close()
            store.close()


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


def _assert_static_ui_contract(case) -> None:
    static_dir = Path(__file__).resolve().parents[3] / "packages/web/src/moex_spread_lab/web/static"
    ui_bundle = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(static_dir.rglob("*")) if path.is_file()
    )
    case.assertNotIn("render-progress-run", ui_bundle)
    case.assertIn('aria-valuemin="0"', ui_bundle)
    case.assertIn('aria-valuemax="100"', ui_bundle)
    case.assertIn("apiStream", ui_bundle)
    case.assertIn("progress-pill", ui_bundle)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
