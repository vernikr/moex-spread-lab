# `msl-web` — execution-proof transport and UI

Layer 4 provides the private research workbench. Under `src/moex_spread_lab/web/`,
`server.py` owns `ScreenerStore`, its lazy `ProofService`, request locks and search
history. `http_handler.py` owns framing, asset resolution and Host/Origin guards;
`dispatch.py` routes requests to `routes_proof.py` / `routes_dev.py` and handles
settings/timeframes. Strategy evaluation, accounting and matched LQDT results
belong to downstream packages, not the UI.

## Run and public surface

From the workspace root:

```sh
MOEX_OFFLINE_ONLY=1 uv run python -m moex_spread_lab.cli screener --root . --port 8765
```

CLI discovery uses `moex_spread_lab.web.cli:register` for `screener` and
`install-workstation-daemon`. Python exports from `moex_spread_lab.web` are
`ScreenerStore`, `make_handler`, `serve`, `strategy_registry_payload`, `STATIC_DIR`
and `VENDOR_DIR`. The selected root supplies local research inputs and preferences;
successful searches are recorded in `proof-searches.jsonl`.

| Method | Route | Contract |
|---|---|---|
| GET | `/`, `/proof` | Workbench; deep links select universe/pair/asset/strategy. |
| GET | `/static/**`, `/vendor/lightweight-charts.standalone.production.js` | Same-origin UI assets. |
| GET | `/api/proof/catalog`, `/api/strategies` | Available windows and strategy schemas. |
| POST | `/api/proof/run`, `/api/proof/optimize`, `/api/proof/plateau`, `/api/proof/windows` | Proof, common-cohort search, diagnostics and window summaries. |
| GET/POST | `/api/settings` | Global research preferences. |
| GET | `/api/timeframes` | Supported timeframes. |
| GET | `/api/dev/health` | Server, cache and worker health. |
| POST | `/api/dev/probe` | Non-persisting timing/profile/worker diagnostics. |

POST bodies must be JSON and fit the route's body limit. Proof POSTs return JSON
unless `X-Progress-Stream: 1` requests NDJSON `progress`, `result` or `error`
records; clients must inspect streamed errors even after HTTP headers succeed.
Disconnected clients cancel expensive work. Concurrent searches receive HTTP 409.

## Locality and presentation

- The CLI binds `127.0.0.1` by default. `MSL_ALLOWED_HOSTS` accepts exact
  comma-separated preview/proxy hosts or `*`; it controls Host/Origin admission,
  not the bind address or authentication. Development routes additionally require
  a loopback peer, local Host and absent/local Origin, regardless of preview opt-ins.
- Responses use `no-store`, `nosniff` and CSP. Scripts and connections are
  same-origin; objects and framing are forbidden.
- `static/index.html`, native acyclic `static/js/` modules and
  `static/css/layout.css` render the API data. Charting uses vendored
  [Lightweight Charts 5.3.0, Apache-2.0](../src/moex_spread_lab/web/vendor/README.md),
  never a CDN.
- Unavailable metrics retain their missing-value semantics and explanations;
  same-bar fills remain diagnostic. Independent window studies are not a shared
  capital portfolio or trading authorization. Numerical rules live in the
  [proof contract](../../proof/docs/execution-proof.md).

## Verification

```sh
uv run pytest packages/web/tests -q
MOEX_OFFLINE_ONLY=1 uv run --with playwright python scripts/run_browser_smokes.py
```

The runner discovers domain suites in `tests/browser/` and supports `--filter`.
Browser checks use isolated synthetic workspaces; install Playwright Chromium
before running them. Set `MOEX_OFFLINE_ONLY=1` in standalone harnesses to prevent
live ISS retrieval. Full delivery verification is `./scripts/verify.py --profile full`.
