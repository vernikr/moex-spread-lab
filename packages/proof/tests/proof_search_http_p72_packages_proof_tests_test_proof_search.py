"""Loopback HTTP boundary checks (P7.2 leaf #131).

The single HTTP regression body is extracted verbatim from
``test_proof_search.py``; the owning test method keeps its node ID.
"""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

from moex_spread_lab.core.settings import ResearchSettings, read_settings
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs
from moex_spread_lab.web.server import ScreenerStore, make_handler
from proof_search_fixtures_p72_packages_proof_tests_test_proof_search import PAIR
from test_execution_proof import _build_root as pair_fixture


def check_http_home_alias_new_search_rerun_and_local_origin_boundary(case):
    proof = case.fixture(pair_fixture)
    inputs = ScreenerInputs(proof.root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ScreenerStore(inputs)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    case.addCleanup(server.server_close)
    case.addCleanup(server.shutdown)
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    case.addCleanup(conn.close)
    pages = []
    for path in ("/?universe=pair_screen", "/proof?universe=calendar_native"):
        conn.request("GET", path)
        response = conn.getresponse()
        case.assertEqual(response.status, 200)
        pages.append(response.read())
    case.assertEqual(pages[0], pages[1])
    case.assertIn(b'id="auto-tune"', pages[0])
    case.assertNotIn("К скринеру пар".encode(), pages[0])
    headers = {"Content-Type": "application/json"}
    conn.request("GET", "/api/settings")
    case.assertEqual(json.load(conn.getresponse()), ResearchSettings().metadata())
    for value, status, origin in [
        (51, 400, None),
        (-1, 400, None),
        (True, 400, None),
        ("25", 400, None),
        (25, 403, "https://example.org"),
        (25, 200, None),
    ]:
        with case.subTest(value=value, origin=origin):
            conn.request(
                "POST",
                "/api/settings",
                json.dumps({"validation_percent": value}),
                {**headers, **({"Origin": origin} if origin else {})},
            )
            r = conn.getresponse()
            r.read()
            case.assertEqual(r.status, status)
            case.assertEqual(
                read_settings(proof.root)["validation_percent"],
                25 if status == 200 else 0,
            )
    conn.request("POST", "/api/proof/run", json.dumps(PAIR), headers)
    plain = json.load(conn.getresponse())
    case.assertEqual(plain["evaluation"]["validation_percent"], 25)
    case.assertNotIn("plateau", plain)
    conn.request(
        "POST",
        "/api/proof/optimize",
        json.dumps(PAIR),
        {**headers, "Origin": "https://example.org"},
    )
    denied = conn.getresponse()
    case.assertEqual(denied.status, 403)
    denied.read()
    conn.request("POST", "/api/proof/optimize", json.dumps(PAIR), headers)
    response = conn.getresponse()
    case.assertEqual(response.status, 200)
    result = json.loads(response.read())
    case.assertIsNotNone(result["selected_parameters"])
    conn.request(
        "POST",
        "/api/proof/run",
        json.dumps(
            {
                **PAIR,
                "parameters": result["selected_parameters"],
                "include_plateau": True,
            }
        ),
        headers,
    )
    response = conn.getresponse()
    case.assertEqual(response.status, 200)
    displayed = json.loads(response.read())
    case.assertEqual(displayed["selection"]["parameters"], result["selected_parameters"])
    case.assertEqual(displayed["plateau"], result["plateau"])
    case.assertEqual(
        result["run_id"],
        json.loads((inputs.history.parent / "proof-searches.jsonl").read_text())["run_id"],
    )
