# `msl-web` (Layer 4) — durable module guide

`packages/web` is the transport and UI layer: a loopback-only HTTP server, JSON
route modules and a same-origin ES-module frontend. It renders and relays; every
number it shows is computed by `msl-proof` under the accounting contracts. Nothing
here authorizes funding, broker access or trading.

Source: `packages/web/src/moex_spread_lab/web/`. Manifest:
`packages/web/manifest.json`. This guide is a durable contract reference, not a
work plan; live status is in GitHub Issues and Project #2. Proof semantics
(scores, lamps, sizing, fills) are owned by the
[proof contract](../../proof/docs/execution-proof.md).

## 1. Routing surface

`dispatch.py` is the single GET/POST table; route modules decide *what* to answer,
never *how* to frame it.

| Route | Method | Owner module | Purpose |
|---|---|---|---|
| `/`, `/proof` | GET | `static_assets.py`, `http_handler.py` | The same workbench page; deep links carry only selection IDs. |
| `/static/**`, `/vendor/**` | GET | `static_assets.py` | Same-origin assets; the vendored chart bundle is served locally. |
| `/api/proof/catalog`, `/api/strategies` | GET | `routes_proof.py` | Universe catalog and the discovered `StrategyPlugin` registry. |
| `/api/proof/run`, `/api/proof/optimize`, `/api/proof/plateau`, `/api/proof/windows` | POST | `routes_proof.py` | Proof run, cohort search, plateau report and window summaries. |
| `/api/settings`, `/api/timeframes` | GET/POST | `routes_settings.py` | Global research preferences and the timeframe registry. |
| `/api/catalog`, `/api/runs`, `/api/searches`, `/api/run`, `/api/screen`, `/api/optimize` | GET/POST | `routes_legacy.py` | Legacy S2 screener compatibility APIs; not used by the proof UI. |
| `/api/dev/*` | GET/POST | `routes_dev.py` | Non-persisting diagnostics (health, probe, pool/worker/RSS metrics). |

`LARGE_PROOF_POST_ROUTES` raises the request-body cap for the chart-bearing proof
routes; every other route keeps the small cap.

## 2. Security and locality contract

- **Loopback by default.** The host/origin guard in `http_handler.py` accepts only
  loopback names. `MSL_ALLOWED_HOSTS` (comma-separated, or `*`) is an explicit
  preview/reverse-proxy opt-in; it never widens the bind address.
- **Same-origin only.** `CSP_HEADER` restricts `default-src`, `script-src` and
  `connect-src` to `'self'`, forbids objects and framing. No CDN is used anywhere;
  the chart bundle is vendored and checksum-pinned in `vendor/README.md`
  (A-14, Lightweight Charts 5.3.0, Apache-2.0).
- **No-store API responses.** JSON/NDJSON answers are uncacheable, and every
  response carries `X-Content-Type-Options: nosniff`.
- **Offline by default.** Web paths use the offline `CalendarData` contract; a
  cache miss fails closed instead of querying MOEX ISS
  (`packages/web/tests/test_offline_catalog.py`).
- **Diagnostics never persist.** `/api/dev/*` runs without writing research
  settings or analytics artifacts.

## 3. Frontend contract

- `static/index.html` plus acyclic native ES modules under `static/js/`. Module
  import cycles are rejected by the architecture gate's frontend DAG test.
- The UI is driven by `/api/strategies` (registry) and `/api/proof/*`; it does not
  hardcode strategy behaviour beyond DOM wiring. Hardcoded built-in IDs are a
  known transitional surface, tracked by the Phase 7 backlog in Epic #12.
- Rendered semantics that must survive refactors: unavailable values stay `null`
  and are explained in tooltips (never rendered as zeros), the period table shows
  `Сд. / п.п./г` only from the current simulation, same-bar reference fills are
  labelled as diagnostic, and the currency figures shown are simulation values
  under the research capital preference — not account balances.
- Ledger CSV export is pinned by `packages/web/tests/test_ledger_csv_contract.py`.

## 4. Boundaries

- `msl-web` is Layer 4: it may consume `msl-core` and `msl-proof`. Direct
  `msl-strategies`/`msl-data` imports survive only to feed the legacy
  `screener_app` compatibility alias and are Phase 7 cleanup scope, not a pattern
  to extend.
- Consequences (capital, risk, fills, costs, validation) are computed downstream;
  a UI change must never alter them. Relaxing a research screen in the dialog does
  not add funding or trading authority.
- The server stays loopback-only. Publishing it means an explicit reverse proxy
  with `MSL_ALLOWED_HOSTS`, not a wider bind.

## 5. Verification

```sh
uv run pytest packages/web/tests -q
uv run --with playwright python packages/web/tests/browser/<name>_smoke.py   # synthetic data
```

The 12 Chromium smoke suites in `packages/web/tests/browser/` are mandatory when a
UI contract changes (`scripts/run_browser_smokes.py`, weekly
`.github/workflows/browser-smoke.yml`). Set `MOEX_OFFLINE_ONLY=1` in any harness.
