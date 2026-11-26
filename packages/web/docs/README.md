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

`dispatch.py` is the single GET/POST table and also owns the small settings and
legacy-screener handlers; route modules decide *what* to answer, never *how* to
frame it.

| Route | Method | Owner module | Purpose |
|---|---|---|---|
| `/`, `/proof` | GET | `static_assets.py`, `http_handler.py` | The same workbench page; deep links carry only selection IDs. |
| `/static/**`, `/vendor/**` | GET | `static_assets.py` | Same-origin assets; the vendored chart bundle is served locally. |
| `/api/proof/catalog`, `/api/strategies` | GET | `routes_proof.py` | Universe catalog and the discovered `StrategyPlugin` registry. |
| `/api/proof/run`, `/api/proof/optimize`, `/api/proof/plateau`, `/api/proof/windows` | POST | `routes_proof.py` | Proof run, cohort search, plateau report and window summaries. |
| `/api/settings`, `/api/timeframes` | GET/POST | `dispatch.py` | Global research preferences and the timeframe registry. |
| `/api/catalog`, `/api/runs`, `/api/searches`, `/api/run`, `/api/screen`, `/api/optimize` | GET/POST | `dispatch.py` | Legacy S2 screener compatibility APIs; not used by the proof UI. |
| `/api/dev/*` | GET/POST | `routes_dev.py` | Non-persisting diagnostics (health, probe, pool/worker/RSS metrics). |

`LARGE_PROOF_POST_ROUTES` raises the request-body cap for the chart-bearing proof
routes; every other route keeps the small cap.

### 1.1 Preserved S2 compatibility surface (do not retire without owner approval)

`README.md`, `packages/proof/docs/execution-proof.md` and `docs/decisions.md` still
name the S2 screener API as a supported compatibility path, so the routes below stay
even though the proof UI never calls them. Inventory recorded by issue #209:

- `/api/run`, `/api/screen`, `/api/optimize` — `dispatch.py` (`LEGACY_POST_ROUTES`,
  `handle_post_legacy`), persisting through `store.save`, `save_screen` and
  `save_search`; `/api/runs` and `/api/searches` read that history back.
- `/api/catalog` — `handle_get_legacy`, fed by the injected `catalog` callable that
  `server.py` binds to
  `moex_spread_lab.strategies.pair_screener.pair_screener.catalog`.
- Backing implementation: `moex_spread_lab.proof.screener_search.{screen_table,
  optimize}` and the root shim `src/moex_spread_lab/screener_search.py`, which
  `packages/proof/tests/test_package_compatibility.py` pins as the same object as
  `moex_spread_lab.proof.screener_search`.
- The web modules import the canonical
  `moex_spread_lab.strategies.pair_screener.pair_screener` symbols directly; the
  former `moex_spread_lab.proof.web_legacy` indirection has no callers left inside
  `packages/web`, so removing that shard does not change any route answer.
- No frontend, CLI or browser-smoke caller proves retirement: the contract is guarded
  by `packages/web/tests/test_screener_app.py` (route answers, error payloads,
  canonical imports, preserved module surface).

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

- `msl-web` is Layer 4: it may consume `msl-core`, `msl-proof` and the shared
  `msl-strategies` pair-screener domain logic that the legacy S2 routes and the
  `screener_app` alias need. `msl-strategies` is still only a transitive dependency
  of `msl-proof` in `packages/web/pyproject.toml` and
  `packages/web/manifest.json`; declaring that edge is module-contract
  reconciliation work, not part of a route or UI change.
- Remaining runtime `msl-proof` imports: `proof.screener_search` (`dispatch.py`),
  `proof.proof_batch`/`proof.proof_search`/`proof.proof_windows` (`routes_proof.py`),
  `proof.proof_pool`/`proof.service` (`store.py`), `proof.strategy_catalog`
  (`strategy_registry.py`) and `proof.phase_metrics`/`proof.proof_pool`/
  `proof.worker_metrics` (`routes_dev.py`).
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
