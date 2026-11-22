# Phase 7 backlog (post-Phase-5 status)

Status: **Step 1–5 of the code-health plan, Phases 0–6 of the modular-architecture plan and Phase 7 package P7.1 are complete (2026-11-23). What remains are Phase 7 packages P7.2–P7.6 from [`modular-architecture-review.md`](modular-architecture-review.md), re-measured after the Phase 4 / Phase 5 splits landed.**

This file records what is *left*, with the measurements taken at `71c0411`, so the next agent does not have to re-derive scope from the pre-split review.

## Completed (no action)

| Plan item | Evidence |
|---|---|
| Code-health Steps 1–3, 5 | `check_health.sh` (6 gates, 18 s), Tier 2/3 CI workflows, `AGENTS.md` rule, workstation `repowise` notes in [`ci.md`](ci.md). |
| Code-health Step 4, Phases 2–5 | `config/code-health-baseline.json`: `files 32`, `complexity 39`; `.basedpyright/baseline.json`: 26 diagnostics; zero exceptions inside `packages/{data,accounting,strategies,proof}/src`. |
| Architecture Phases 0–6 | Six-package acyclic workspace, plugin protocol for all four families, module-split frontend, path-aware CI matrix. |
| Phase 7 F1 | `proof_runtime/service.py` is 499 LOC / 6.7k tokens (was 2,331 LOC / 31k). |
| Phase 7 F6 (frontend budget part) | Every `static/js/*.js` module is under its 400-LOC / 3,500-token ratchet after the Phase 5 split (largest: `charts.js` 484 LOC, `strategy-form.js` 472, `picker.js` 417). |
| Phase 7 P7.1 (F3 zero-touch drop-in) | `packages/strategies/pyproject.toml` declares no built-in strategy entry point; `test_architecture_dag.py` classifies and isolation-checks `strategies/<folder>/` from disk (`295` LOC / `4,129` tokens, under its ratchet); guards: `packages/core/tests/test_architecture_dag_dropin.py`, `packages/strategies/tests/test_strategy_dropin_contract.py`. |
| Phase 7 P7.1 (F4 lazy registry) | `proof_runtime/strategy_catalog.py` owns `get_strategy_registry()` / `get_proof_adapter_ids()` / `get_strategy_axes()` / `clear_strategy_registry_cache()`; `proof_kernel.py` no longer assigns a registry, an id set or an axes map at import (`659` LOC / `8,166` tokens); `registry=` injection in `strategy_search`, `_prepare_cohort`, `checked_plateau`, `_validate`, `_native_request`, `_active_seed`, `StudyWindow`. |

## Remaining backlog

Ordered by value per unit of risk. Sizes are rough; every item keeps the Stage-9 invariants (`run_id` parity, `test_perf_equivalence.py`, CLI `--help` hashes, `/api/*` schemas, the 11 Playwright smokes).

### 1. F2 — unify the four built-ins onto the intent-adapter pipeline (L, high risk)

Hardcoded built-in ids still live in 5 source files: `web/routes_proof.py` and `web/strategy_registry.py` (`_BUILTIN_STRATEGY_IDS`), `static/js/strategy-form.js` (`SOLO_BUILTIN_IDS`), `static/js/strategy-registry.js` (`FALLBACK_STRATEGIES`), plus the `proof_runtime` translate path. Built-ins bypass their own adapters while drop-in plugins go through `calendar_price_intents.v1` / `pair_price_intents.v1` / `carry_settlement_intents.v1`.

Move one family at a time (calendar first, it has the tightest parity tests), prove `test_perf_equivalence.py` bit-for-bit and `run_id` parity at each step, and only then delete the translate branches. Do not attempt all four in one change.

### 2. F6 — remaining `packages/proof` code-health debt (M–L, medium risk)

Still over budget and still carrying every remaining typing diagnostic:

| File | LOC | tokens | baseline diagnostics |
|---|---|---|---|
| `proof_runtime/proof_batch.py` | 807 | 8,836 | part of the 23 `proof` entries |
| `proof_runtime/proof_kernel.py` | 659 | 8,166 | 〃 |
| `proof_runtime/proof_pool.py` | 670 | 7,805 | 〃 |
| `proof_runtime/screener_search.py` | 384 | 4,682 | 〃 |
| `proof_runtime/service.py` | 498 | 6,701 | 〃 (`ProofService::prepare` cog 38, `_prepare_pair` cog 29) |

Top complexity outliers: `proof_batch._prepare_cohort` 79, `carry_intent_accounting.replay_carry_intents` 73, `pair_plugin_accounting.prepare_daily_pair_unit` 52, `proof_pool.CohortWorkerPool.eval_point` 41, `screener_search.optimize` 40, `proof_strategy_search.strategy_search` 35, `pair_funding_replay.replay_pair_funding` 36, `core/w0.py::validate_manifest` 28.

Suggested order: `service.py` (smallest, highest traffic) → `proof_kernel.py` → `proof_pool.py` → `proof_batch.py`. Split by spec/engine boundary as in Phase 4, then `--update-baseline`. Enabling `strict` typing for `packages/proof/src` is the last step, after the file splits.

### 3. F5 — namespace and boundary polish (M–L, high blast radius — defer)

- `packages/proof` uses `moex_spread_lab.proof_runtime` instead of `moex_spread_lab.proof` because of the legacy `moex_spread_lab.proof` module; renaming requires a migration window for both names.
- Concrete MOEX tickers are split across layers: 287 lines of `PAIRS` / `GLD_HISTORICAL_FAR` in `core/universe.py`, `ASSETS` / `CROSS_EXPIRY_PAIRS` in `strategies/calendar/contracts.py`, and a 3-line re-export in `data/universe/__init__.py`.
- `web/server.py` still imports `funding_carry.carry.fetch_history` and `pair_screener` internals to feed the `screener_app` compatibility alias.

Do this only alongside F2, because both edit the same import edges.

### 4. F7 — hygiene leftovers (S each)

- `packages/core/tests/test_w0.py` imports Layer 1 `moex_spread_lab.accounting.research`, so `uv run --package msl-core pytest packages/core/tests` fails in isolation. Move that assertion into `packages/accounting/tests` (or guard it with `pytest.importorskip`).
- `scripts/architecture_gate.py` claims to run in a bare checkout but pulls in `test_strategy_conformance.py`, which eagerly imports `pyarrow` through `accounting`. Lazy-import the conformance test inside the gate.
- Co-located `docs/` are missing for `packages/core`, `packages/proof` and `packages/web`; `w0.md`, `execution-proof.md`, `performance-optimization.md`, `screener-s1.md`, `screener-s2.md`, `remote-agent-testing.md` still live in root `docs/development/`.

## P7.1 zero-touch strategy contract (2026-11-23)

Adding a strategy is one new folder — `packages/strategies/src/moex_spread_lab/strategies/<subpkg>/{__init__,engine,plugin}.py` exporting `PLUGIN` — and nothing else:

- `discover_strategies()` finds the folder with `pkgutil.iter_modules`. The manifest declares no per-strategy entry point; entry points stay reserved for external wheels installed outside `msl-strategies`.
- `test_architecture_dag.py` classifies every `strategies/<folder>/` into Layer 2 from disk and applies the cross-subfolder import ban. A strategy may import `core`, `data`, `accounting`, `strategies.base`, `strategies.calendar_eval` and its own subpackage — nothing from a sibling strategy.
- Proof participation is declared by the plugin itself: `proof_adapter`, a unique `family_id`, the full three-fill price set and one universe. `get_proof_adapter_ids()` then routes it into `/api/proof/optimize` and `get_strategy_axes()` derives its tunable axes.
- Long-lived processes refresh discovery through `get_strategy_registry(refresh=True)` (`/api/strategies`, `/api/proof/catalog`); a proof run keeps the registry it started with, so one run stays deterministic. Tests inject `registry=` instead of patching module globals.

Verified on 2026-11-23 with a throw-away `probe_demo/` folder (a self-contained copy of the calendar z-score engine with its own ids and `proof_adapter = "calendar_price_intents.v1"`):

| Check | Result |
|---|---|
| `git status --porcelain` | only the new folder (plus the P7.1 changes themselves) |
| `test_architecture_dag.py`, `test_strategy_conformance.py` | green, no edit to either |
| `/api/strategies` (`strategy_registry_payload`) | `is_builtin: false`, `proof_enabled: true` |
| proof parity | same `run_id` trades, ledger and reconciliation as `zscore_mean_reversion` |
| `/api/proof/optimize` | searched `["zscore_mean_reversion", "convergence_to_ruonia", "probe_demo"]`, 240 trials in 2.1 s, probe selected |

Two deliberate deviations from the P7.1 plan text: the probe cannot be named `_probe_demo` (discovery skips `_`-prefixed directories), and its plugin must not subclass another strategy's plugin class (that is the cross-subfolder import the DAG test rejects). `research_run()` keeps seven parameters instead of gaining `registry=`, because `PLR0913` caps arguments at seven; injection happens one level lower in `strategy_search`, `batch_search`, `checked_plateau` and `_prepare_cohort`.

## Known environment caveats

- Tier 2 (`clang-tidy` + ASan/UBSan) needs `clang`; the Linux sandbox has none, so Tier 2 evidence comes from CI only.
- ~~`/api/proof/catalog` takes ~185 s on the synthetic fixtures~~ — **fixed by P0 of [`agent-performance-plan.md`](agent-performance-plan.md)**: cold catalog on `two_calendar_windows` is now ~0.05 s with no outbound sockets (`packages/web/tests/test_offline_catalog.py` guards it). Browser smokes still use `wait_until="load"` and generous per-call timeouts; `networkidle` may never settle.
- The bridge `bash` tool kills its process tree when a call times out, so long-lived servers must be started with the bridge `start_process` tool.
