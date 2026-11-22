# Post-Migration Architectural Review & Phase 7 Follow-Up Plan

Status: **Architectural Review & Phase 7 Follow-Up Plan** (2026-11-22, evaluated at `origin/main` `036a8c8`). No runtime code or tests were modified in this turn.

---

## 1. Executive Summary & Overall Assessment

Between baseline commit `aed0de1` and `036a8c8` (38 commits), the repository completed **Phases 0–6** of [`modular-architecture-plan.md`](modular-architecture-plan.md) alongside **Steps 1–3, 5, and Layer 1–2 of Step 4** of [`code-health-plan.md`](code-health-plan.md).

### Verdict: Strong, Production-Grade Modularization (~85% of Ideal End-State Achieved)

> **Status update (2026-11-22, `71c0411`):** F1 and the frontend half of F6 are done (Phase 4 split `service.py` 2,331 -> 499 LOC, Phase 5 split the frontend to 18 modules). The live, re-measured remainder of this backlog is tracked in [`phase7-backlog.md`](phase7-backlog.md); the tables below are the pre-split snapshot kept for history.


The core structural transformation succeeded without breaking numerical determinism, Stage-9 accounting invariants, or external CLI/HTTP compatibility:

- **Strict 6-Layer Acyclic Workspace (`packages/*`):** All 6 workspace packages (`msl-core`, `msl-data`, `msl-accounting`, `msl-strategies`, `msl-proof`, `msl-web`) are active PEP 420 implicit namespace packages (`0` stray `packages/*/src/moex_spread_lab/__init__.py` files), verified by `packages/core/tests/test_architecture_dag.py` (`Tarjan max SCC == 1` across all modules including function-level lazy imports, strict `data ⊥ accounting` sibling isolation, and zero workspace imports of root compatibility shims).
- **Zero-Baseline Code Health in Layers 1 & 2:** Every source file in `packages/data/src` (35 files), `packages/accounting/src` (18 files), and `packages/strategies/src` (59 files) now has **zero** LOC/token overrides, **zero** cognitive-complexity outliers (`<= 15`), **zero** `basedpyright` baseline errors, and **zero** Ruff per-file ignores.
- **Pluggable CLI & Frontend Decomposition:** Root `src/moex_spread_lab/cli.py` shrank from `1,055` LOC (`cog=343`) to **29 lines** of deterministic `entry_points(group="moex_spread_lab.cli")` discovery with byte-verified `--help` hashes (`cli_help_hashes.json`). Monolithic `proof_ui.html` (`1,176` LOC) was replaced by `static/index.html` (`174` LOC), 4 CSS files, and 12 acyclic native ES modules (`static/js/*.js`) with `GET /api/strategies` (`strategy-registry.v1`).
- **Conflict-Free CI & C99 Safety:** `.github/test-durations.json` is gone (replaced by 6-package dynamic `xdist --dist loadfile` matrix jobs), and C99 `_fastcore.c` now passes `clang-tidy` + ASan/UBSan/LSan equivalence gates.

### What Still Needs Improvement (Why Multi-Agent Friction Remains in Layers 2–4)

Despite the clean package boundaries, **seven concrete architectural and multi-agent bottlenecks** remain concentrated in **`packages/proof` (Layer 3)**, **`packages/strategies` (Layer 2)**, and **`packages/web` (Layer 4)**:

| # | Gap / Bottleneck | Severity | Evidence in `036a8c8` | Multi-Agent / Maintenance Impact |
|---|---|---|---|---|
| **F1** | **`proof_runtime/service.py` grew into a 2,445-line (`2,331` non-blank LOC, ~30.9k tokens) super-monolith** | **High** | Was `1,580` LOC in legacy `proof.py`; grew +55% as plugin adapters (`_prepare_daily_pair_plugin`, `_fixed_daily_pair_plugin_payload`, carry plugin branches) were added alongside legacy engines. Holds 9 cognitive-complexity outliers (up to `cog=113`) and 16 `basedpyright` baseline errors. | Single biggest merge-conflict magnet and LLM context-window blowup in the repository. Parallel work on pair proof, calendar proof, carry proof, or catalog collides in one 31k-token file. |
| **F2** | **Dual-path execution in `msl-proof` & `msl-web` (4 built-ins vs drop-in plugins)** | **High** | Built-in strategies (`mean_reversion`, `funding_only`, `zscore_mean_reversion`, `convergence_to_ruonia`) bypass their own `StrategyPlugin` adapters and run through bespoke legacy codepaths, while custom plugins run through `calendar_price_intents.v1`, `pair_price_intents.v1`, and `carry_settlement_intents.v1`. Hardcoded built-in IDs still appear across 11 files in `proof_runtime` and `web` (`_translate`, `_BUILTIN_STRATEGY_IDS`, `SOLO_BUILTIN_IDS`, `FALLBACK_STRATEGIES`). | Maintaining two parallel accounting/UI pipelines doubles surface area and means improvements to plugin adapters are not dogfooded by the 4 primary strategies (and vice versa). |
| **F3** | **Adding a built-in strategy subfolder still touches 2 shared files in `packages/strategies` & `packages/core`** | **Medium-High** | 1) `packages/strategies/pyproject.toml` hardcodes per-strategy `[project.entry-points."moex_spread_lab.strategies"]` even though `discover_strategies()` already auto-discovers subpackages via `pkgutil.iter_modules`!<br>2) `packages/core/tests/test_architecture_dag.py` hardcodes `_LAYER_SPECS["strategies"]` module names and a fixed `plugins = {...}` set (`lines 48–55, 263–269`), so adding a new strategy folder **fails the DAG test** until `packages/core/tests/test_architecture_dag.py` is edited! | Directly breaks the "zero shared-file edits when two agents add strategies in parallel" goal: two branches adding strategies conflict in `packages/strategies/pyproject.toml` and `packages/core/tests/test_architecture_dag.py`. |
| **F4** | **Import-time mutable global `STRATEGY_REGISTRY` in `proof_kernel.py`** | **Medium** | `proof_kernel.py:95–122` executes `STRATEGY_REGISTRY = discover_strategies()`, `PROOF_ADAPTER_IDS`, and `STRATEGY_AXES` at **module import time**, while `service.py` and `routes_proof.py` call `discover_strategies()` per request. | Causes stale registry state in long-lived processes/tests unless callers monkey-patch three module globals (`STRATEGY_REGISTRY`, `PROOF_ADAPTER_IDS`, `STRATEGY_AXES`), and triggers eager Layer-2 imports during worker startup. |
| **F5** | **Namespace & boundary inconsistencies (`proof_runtime` vs `proof`; `universe.py` in `core`; `msl-web` transitional re-exports)** | **Medium** | 1) `packages/proof` uses `moex_spread_lab.proof_runtime` instead of `moex_spread_lab.proof` solely because of root shim naming, deviating from the `core`/`data`/`accounting`/`strategies`/`web` convention.<br>2) `packages/data/.../data/universe/__init__.py` is a 3-line re-export while 287 lines of concrete MOEX contract tickers (`PAIRS`, `GLD_HISTORICAL_FAR`) sit in `msl-core/universe.py`, and `ASSETS`/`CROSS_EXPIRY_PAIRS` sit in `msl-strategies/calendar/contracts.py`.<br>3) `packages/web/server.py` (`__all__` of 42 symbols) still imports `funding_carry.carry.fetch_history` and `pair_screener` internals just to feed the `screener_app` compatibility alias. | Blurs domain ownership: editing pair tickers touches Layer 0 (`msl-core`); `msl-web` depends directly on `msl-strategies` internals instead of talking strictly to `msl-proof` + `msl-core`. |
| **F6** | **Remaining Layer 3 & Layer 4 Code-Health Debt (`proof`, `web`, `_fastcore.c`)** | **Medium** | All 34 remaining `basedpyright` source diagnostics live exclusivamente in `packages/proof` (27) and `packages/web` (7). Four proof files (`service.py` `2,331`, `proof_batch.py` `807`, `proof_kernel.py` `713`, `proof_pool.py` `683` LOC) and four frontend JS modules (`charts.js` `573`, `strategy-form.js` `548`, `picker.js` `522`, `ledger.js` `493` LOC) exceed the 400-LOC / 3,500-token AI-readability budget. | Agents working in `proof` or `web` do not yet have strict static typing (`strict` is only on `packages/core/src`) or `<400`-LOC file budgets. |
| **F7** | **Minor Hygiene & Standalone Gate Coupling** | **Low-Medium** | 1) `scripts/architecture_gate.py` advertises working from a plain checkout without third-party deps, yet imports `test_strategy_conformance.py`, which eagerly imports `accounting/__init__.py -> ledger.py -> run_artifacts.py -> pyarrow` (`ModuleNotFoundError` outside `.venv`).<br>2) `packages/core/tests/test_w0.py` (Layer 0 test!) imports Layer 1 `moex_spread_lab.accounting.research`, violating upward test isolation (`uv run --package msl-core pytest packages/core/tests` fails if `msl-accounting` isn't installed).<br>3) Co-located `docs/` are still missing for `packages/core`, `packages/proof`, and `packages/web` (`w0.md`, `execution-proof.md`, `performance-optimization.md`, `screener-s1.md`, `screener-s2.md`, `remote-agent-testing.md` remain in root `docs/development/`). | Breaks true single-package isolation (`--package msl-core`) and leaves documentation ownership split inconsistently across layers. |

---

## 2. Detailed Phase 7 Improvement Plan

The improvements below are designed as **six orthogonal, non-breaking work packages (P7.1 – P7.6)** that preserve every Stage-9 invariant (`run_id` parity, `test_perf_equivalence.py`, CLI flags, `/api/*` JSON/NDJSON schemas, and 11 Playwright smoke suites).

---

### Work Package P7.1 — True Zero-Touch Strategy Drop-In & Lazy Registry

**Goal:** Ensure two agents creating two new strategy folders in parallel branches touch **zero** shared tracked files anywhere in the repository, and eliminate import-time registry globals.

**Concrete Actions:**
1. **Dynamic Subpackage Classification in `test_architecture_dag.py` (`packages/core/tests/test_architecture_dag.py`):**
   - Replace the static module enumeration for `strategies.*.*` in `_LAYER_SPECS["strategies"]` and the hardcoded `plugins = {"pair_mean_reversion", ...}` set in `test_strategy_plugin_subfolders_are_strictly_isolated()` with filesystem-driven discovery:
     - Any subpackage `packages/strategies/src/moex_spread_lab/strategies/<subpkg>/` not in `_SHARED_STRATEGY_INFRA = {"calendar", "pair_screener"}` is **automatically** classified into Layer `strategies` and **automatically** subject to strict cross-subfolder import isolation.
   - Result: adding `strategies/kalman_spread/` requires **zero edits** to `test_architecture_dag.py`.
2. **Remove Redundant Built-In Entry Points from `packages/strategies/pyproject.toml`:**
   - `discover_strategies()` already discovers all `moex_spread_lab.strategies.<subpkg>.plugin:PLUGIN` via `pkgutil.iter_modules`, while keeping `importlib.metadata.entry_points(group="moex_spread_lab.strategies")` for *external* wheel plugins installed outside `msl-strategies`.
   - Removing the 4 duplicate lines in `packages/strategies/pyproject.toml` eliminates `pyproject.toml` + `uv.lock` churn and merge conflicts when adding internal strategies.
3. **Replace Import-Time Globals in `proof_kernel.py` with Cached Registry Accessor:**
   - Replace `STRATEGY_REGISTRY = discover_strategies()`, `PROOF_ADAPTER_IDS`, and `STRATEGY_AXES` at module top-level with `get_strategy_registry()` / `get_proof_axes(registry=None)` backed by a lightweight invalidatable cache (`clear_strategy_registry_cache()`).
   - Thread `registry` explicitly through `strategy_search()`, `_prepare_cohort()`, and `ProofService` so unit tests pass custom registries via dependency injection instead of `unittest.mock.patch` on module globals.

**Acceptance Gate:**
- Creating a brand-new folder `packages/strategies/src/moex_spread_lab/strategies/_probe_demo/{__init__,engine,plugin}.py` passes `test_architecture_dag.py`, `test_strategy_conformance.py`, `/api/strategies`, and `/api/proof/optimize` with `git diff --name-only` showing **only** files inside `_probe_demo/`.

---

### Work Package P7.2 — Decompose `packages/proof` (`service.py` & Cohort Modules) + Strict Typing

**Goal:** Complete Step 4 of `code-health-plan.md` for Layer 3 (`packages/proof`), eliminating the 2,445-line `service.py` hotspot, all 27 `basedpyright` errors in `proof`, and all `>400` LOC / `>15` cognitive-complexity outliers.

**Target Submodule Structure inside `packages/proof/src/moex_spread_lab/proof_runtime/`:**

```text
packages/proof/src/moex_spread_lab/proof_runtime/
├── __init__.py
├── cli.py                       # Entry-point CLI registrations
├── caches.py                    # _BENCHMARK_MARKS_CACHE, _INSTRUMENTS_CACHE, _SHA256_CACHE, _clean, _mark (~150 LOC)
├── service.py                   # Thin ProofService facade: catalog(), proof(), prepare(), worker_pool() (~240 LOC)
├── adapters/
│   ├── calendar_proof.py        # Calendar replay wrapper, _calendar_proof, _calendar_provenance, _calendar_instrument (~260 LOC)
│   ├── pair_mr_proof.py         # Ordinary pair price-replay & _mean_reversion_proof decomposed into <15 cog helpers (~320 LOC)
│   ├── pair_carry_proof.py      # EOD funding carry replay, _funding_proof, _funding_ledger (~310 LOC)
│   ├── pair_plugin_accounting.py# Existing validated pair-intent adapter (refactored to cog <= 15) (~240 LOC)
│   ├── carry_intent_accounting.py# Existing settlement-intent adapter (refactored to cog <= 15) (~240 LOC)
│   └── pair_funding_replay.py   # Existing causal daily funding resolver (refactored to cog <= 15) (~200 LOC)
├── payload/
│   ├── pair_payload.py          # _pair_payload, _trade_record, _price_ledger, reconciliation summary (~280 LOC)
│   └── formatting.py            # Shared trade/fill/note builders (~150 LOC)
├── search/
│   ├── kernel.py                # Axis, StudyWindow, seed derivation, annualization (~320 LOC)
│   ├── landscape.py             # Landscape, _tune, _lamps, _measure (~300 LOC)
│   ├── cohort_prepare.py        # Target validation, _native_request, _prepare_cohort (~340 LOC)
│   ├── cohort_eval.py           # _Cohort, _RemoteCohort, checked_plateau, _cohort_result (~330 LOC)
│   ├── strategy_search.py       # Cross-family common-strategy search (~280 LOC)
│   ├── windows.py               # Read-only window_summaries (~110 LOC)
│   └── single_search.py         # research_run & JSONL search record builder (~180 LOC)
├── pool/
│   ├── worker_pool.py           # CohortWorkerPool lifecycle, sticky sharding, canary fallback (~340 LOC)
│   └── worker_tasks.py          # Spawn worker entrypoints, _worker_init_cohort, _estimate_weight (~280 LOC)
├── legacy_s2/
│   ├── screener_search.py       # S2 table & neighborhood grid search (~350 LOC)
│   └── audit.py                 # Independent P&L recomputation (~180 LOC)
```

**Concrete Actions:**
1. Split `service.py` (`2,331` non-blank LOC), `proof_batch.py` (`807` LOC), `proof_kernel.py` (`713` LOC), and `proof_pool.py` (`683` LOC) along the responsibilities above while keeping `proof_runtime.{service,proof_batch,proof_kernel,proof_pool,proof_search,proof_strategy_search,proof_windows,screener_search,audit}` thin re-export facades so zero external/test imports break.
2. Resolve all 27 `basedpyright` diagnostics in `packages/proof` and add `"packages/proof/src"` (plus `"packages/data/src"`, `"packages/accounting/src"`, `"packages/strategies/src"`) to `[tool.basedpyright].strict` in `pyproject.toml`.
3. Run `uv run python scripts/code_health_gate.py --update-baseline` to ratchet `packages/proof/src` file and complexity baselines down to **zero**.

**Acceptance Gate:**
- `uv run pytest packages/proof/tests -q -n auto --dist loadfile` (including `test_perf_equivalence.py` and `test_fastcore_parity.py`) 100% green with identical `run_id`s.
- Zero `packages/proof/src/*` entries left in `config/code-health-baseline.json`, `.basedpyright/baseline.json`, and `[tool.ruff.lint.per-file-ignores]`.

---

### Work Package P7.3 — Unify Built-In Strategies onto the Validated Intent-Adapter Pipeline

**Goal:** Eliminate the dual-maintenance split between the 4 legacy built-in strategy codepaths and the generic `StrategyPlugin` intent adapters, removing hardcoded strategy-ID switchboards across `proof` and `web`.

**Concrete Actions:**
1. **Declarative Parameter Aliasing & Adapter Metadata on `StrategyPlugin` (`msl-core/contracts.py`):**
   - Add optional declarative attributes to `StrategyPlugin` (with safe defaults):
     - `proof_adapter: str | None = None` (promoting the currently untyped `getattr(plugin, "proof_adapter", None)` into the formal `Protocol` — immediately fixing multiple `reportAttributeAccessIssue` errors in `basedpyright`!);
     - `family_parameter_aliases: Mapping[str, str] = {}` (e.g., `window <-> lookback` within `family_id = "mean_reversion"`, replacing hardcoded `_translate()` in `proof_batch.py`);
     - `ui_field_order: tuple[str, ...] | None = None` (replacing hardcoded `_UI_FIELD_ORDER` in `web/routes_proof.py`);
     - `required_asset_capability: str | None = None` (e.g., `"convergence"`, replacing hardcoded `if strategy == "convergence_to_ruonia"` in `proof_batch.py`).
2. **Parity-Verified Cutover of Built-Ins (One Strategy at a Time):**
   - Verify bit-for-bit `execution-proof.v1` parity (including `notes`, `price_role`, `provenance`, `series`, `trades`, `ledger`, `summary`, and `run_id`) between each built-in's legacy path and its `StrategyPlugin` adapter (`calendar_zscore`, `calendar_ruonia`, `pair_mean_reversion`, `funding_carry`).
   - Where built-in provenance notes differ from third-party plugin notes (e.g., built-in carry note vs trusted-plugin disclaimer), drive that from a declarative `trust_tier: Literal["builtin", "plugin"] = "plugin"` attribute on `StrategyPlugin` rather than duplicating 400 lines of replay logic.
3. **Frontend Schema Unification (`strategy-form.js` & `state.js`):**
   - Drive solo and cohort DOM mounting from one unified `/api/strategies` schema path (keeping `SOLO_BUILTIN_IDS` strictly as a tiny optional DOM-ID map if needed by legacy selectors in browser tests, and trimming `FALLBACK_STRATEGIES` in `state.js` to minimal bootstrap stubs).

**Acceptance Gate:**
- Built-in and custom plugins share one validated replay & accounting pipeline per adapter class (`calendar_price_intents.v1`, `pair_price_intents.v1`, `carry_settlement_intents.v1`) with **100% bit-for-bit `run_id` parity** on all existing golden/perf-equivalence fixtures.

---

### Work Package P7.4 — Complete Layer 4 (`packages/web`) Decomposition & Boundary Purity

**Goal:** Make `msl-web` a pure Layer 4 HTTP/UI transport package that depends only on `msl-core` and `msl-proof` (removing direct `msl-strategies` / `msl-data` leakage) and bring its 4 oversized JS modules under the 400-LOC AI-readability budget.

**Concrete Actions:**
1. **Move Legacy `screener_app` Compatibility Plumbing Out of `web/server.py`:**
   - Currently `packages/web/src/moex_spread_lab/web/server.py` imports 30+ symbols from `moex_spread_lab.core.universe`, `funding_carry.carry`, and `pair_screener` solely because `src/moex_spread_lab/screener_app.py` does `sys.modules[__name__] = server`.
   - Move those legacy re-exports and `_history_source` wrapper into `src/moex_spread_lab/screener_app.py` itself (a 45-line compatibility module delegating HTTP server functions to `moex_spread_lab.web.server` and pair-screener helpers to `moex_spread_lab.strategies.pair_screener`).
   - Shrink `web/server.py` to ~190 LOC (`cog <= 15`) with dependencies restricted to `msl-core` and `msl-proof` (plus `routes_legacy.py` calling a clean facade on `msl-proof`), and fix all 7 `basedpyright` diagnostics in `packages/web` so `"packages/web/src"` joins `strict` mode.
2. **Split the 4 Frontend JS Modules Exceeding 400 LOC (`packages/web/.../static/js/`):**
   - `charts.js` (`573` LOC) -> `charts-panes.js` (Lightweight Charts setup & sync) + `charts-tooltips.js` (crosshair tooltips & markers) + `charts.js` (~220 LOC each).
   - `strategy-form.js` (`548` LOC) -> `strategy-controls.js` (DOM field builder & solo/cohort sync) + `strategy-form.js` (request building & validation).
   - `picker.js` (`522` LOC) -> `window-table.js` (`Период / V / C / Сд. / п.п./г` table & streaming `/api/proof/windows` reader) + `picker.js` (asset/branch tree & checkbox cascade).
   - `ledger.js` (`493` LOC) -> `ledger-csv.js` (CSV export & summary tiles) + `ledger.js` (daily journal table & expandable fill cards).
   - `test_architecture_dag.py::test_frontend_es_module_import_dag_is_acyclic` automatically verifies zero cycles across all split ES modules!

**Acceptance Gate:**
- Zero `packages/web/src/*` entries in `config/code-health-baseline.json` and `.basedpyright/baseline.json`; all 11 Playwright Chromium smoke suites pass headless.

---

### Work Package P7.5 — Domain Boundary Polish (`universe` Consolidation & Package Isolation)

**Goal:** Fix remaining cross-layer boundary leaks so every workspace package can be tested, type-checked, and reasoned about in true isolation.

**Concrete Actions:**
1. **Decouple `packages/core/tests` from `msl-accounting` (`F7`):**
   - Move the three W1–W7 research-pipeline tests (`test_research_waves_pass_fixture_gates_but_remain_inconclusive`, `test_execution_replays_stale_and_partial_paths`, `test_matched_ledger_starts_at_zero_and_has_no_top_up`) out of `packages/core/tests/test_w0.py` into `packages/accounting/tests/test_research_waves.py` (where `run_research_pipeline` actually lives!).
   - Add a DAG check in `test_architecture_dag.py` verifying **package test layer discipline**: `packages/<pkg>/tests` may only import `moex_spread_lab.<allowed_layers_for_pkg>` (never upward layers!).
2. **Make `scripts/architecture_gate.py` Genuinely Stdlib-Only:**
   - Lazy-import `pyarrow` inside `run_artifacts.py` functions (or split `accounting/__init__.py` eager exports, or extract the pure discovery/drop-in test into `packages/strategies/tests/test_plugin_discovery_gate.py` without importing `build_carry_study` / `accounting.__init__`).
   - Result: `python3 scripts/architecture_gate.py` runs in `<0.5 s` even in bare Python containers without `pyarrow`/`duckdb`/`optuna` installed.
3. **Unify Instrument & Pair Universe Catalog in `msl-data.universe` (`F5`):**
   - Keep generic universe ID constants (`PAIR_UNIVERSE`, `CALENDAR_UNIVERSE`) in `msl-core`, and consolidate concrete MOEX instrument catalogs (`PAIRS`, `HISTORICAL_FAR_BY_NEAR`, `ASSETS`, `CROSS_EXPIRY_PAIRS`, `SYNTHETIC_TRIANGLE_PAIRS`, `contract_chain`) into `packages/data/src/moex_spread_lab/data/universe/` (with re-exports in `core.universe` and `strategies.calendar.contracts` for full backward compatibility).
   - Now adding new MOEX futures maturities or asset metadata happens in **one** domain folder (`packages/data/.../universe/`), never touching Layer 0 `msl-core`.
4. **Canonicalize `moex_spread_lab.proof` Namespace (Optional / Non-Breaking):**
   - Expose `packages/proof/src/moex_spread_lab/proof/` as the canonical namespace matching `core`/`data`/`accounting`/`strategies`/`web` while keeping `moex_spread_lab.proof_runtime` as a thin internal alias and updating root `src/moex_spread_lab/proof.py` compatibility handling cleanly.

**Acceptance Gate:**
- `python3 scripts/architecture_gate.py` passes under bare stdlib Python (`PYTHONPATH=...`).
- `uv run --isolated --package msl-core pytest packages/core/tests` passes with zero higher-layer packages installed.

---

### Work Package P7.6 — Complete Docs Co-Location & Module Manifest Conformance

**Goal:** Finish documentation co-location for `core`, `proof`, and `web`, and validate `packages/*/manifest.json` mechanically so manifests never drift from reality.

**Concrete Actions:**
1. **Co-Locate Remaining Module-Specific Docs (`git mv` + link updates in `README.md`):**
   - `docs/development/w0.md` -> `packages/core/docs/w0.md`
   - `docs/development/execution-proof.md`, `docs/development/performance-optimization.md`, `docs/development/profit-audit.md` -> `packages/proof/docs/`
   - `docs/development/screener-s1.md`, `docs/development/screener-s2.md`, `docs/development/remote-agent-testing.md` -> `packages/web/docs/`
   - Keep truly cross-cutting governance and repository-wide plans (`ci.md`, `modular-architecture-plan.md`, `modular-architecture-review.md`, `code-health-plan.md`, `research-gate.md`, `contract-history-audit.md`, `point-in-time-evidence-audit.md`) in root `docs/development/`.
2. **Automated `packages/*/manifest.json` Validation in `architecture_gate.py`:**
   - Assert that every `packages/<name>/manifest.json` parses cleanly, matches `packages/<name>/pyproject.toml` `version`, declares `dependencies` matching `[project].dependencies` (`msl-*` workspace edges), and obeys Stage-5 required manifest keys (`module_id`, `release`, `contract_version`, `kind`, `capabilities`, `inputs`, `outputs`, `dependencies`, `owner`, `determinism`).

**Acceptance Gate:**
- Every workspace package (`core`, `data`, `accounting`, `strategies`, `proof`, `web`) has a populated `docs/`, `tests/`, `src/`, `pyproject.toml`, and validated `manifest.json`; zero broken markdown links across `README.md` and `docs/`.

---

## 3. Recommended Execution Order & Parallelism Map

Because Phases 0–6 already isolated the workspace packages, **most of Phase 7 can itself be executed by parallel agents without merge conflicts**:

```text
Step A (Sequential prerequisite, ~30 min):
  └── P7.1 (Dynamic DAG subpackage discovery + lazy StrategyRegistry + Protocol `proof_adapter` field)
        │
        ├──> Agent 1 (packages/proof):      P7.2 — Split `service.py` / `proof_batch` / `proof_kernel` / `proof_pool` + strict typing
        ├──> Agent 2 (packages/web):        P7.4 — Clean `web/server.py` boundary + split 4 JS modules <400 LOC + strict typing
        └──> Agent 3 (core/data/docs):      P7.5 + P7.6 — Test layer isolation, stdlib `architecture_gate.py`, `universe` & docs co-location
                │
                v
Step B (After P7.2 + P7.4 land):
  └── P7.3 — Unify built-in strategies onto the validated intent-adapter pipeline with bit-for-bit `run_id` parity
```

### Target End-State Metrics After Phase 7

| Metric | Before Migration (`aed0de1`) | Current State (`036a8c8`) | Target After Phase 7 |
|---|---:|---:|---:|
| Import cycles across repo (incl. lazy imports) | 11 mutual / 2 large SCCs | **0** | **0** (+ test-layer upward import ban) |
| Shared files touched to add an internal strategy | 7 files | 2 files (`pyproject.toml`, `test_architecture_dag.py`) | **0 files** (pure folder drop-in) |
| Largest Python / JS source file (non-blank LOC) | `1,580` LOC (`proof.py`) | `2,331` LOC (`proof_runtime/service.py`) | **`<= 400` LOC** across all 6 packages |
| Production Python source files in `code-health-baseline.json` | 53 files | 8 files (`core`: 1, `proof`: 6, `web`: 1 + 4 JS) | **0 Python/JS source files** (only C99 `_fastcore.c` if kept single-TU) |
| `basedpyright` strict-mode packages | 0 / 6 | 1 / 6 (`core`) | **6 / 6 (`core`, `data`, `accounting`, `strategies`, `proof`, `web`)** |
| `.basedpyright/baseline.json` unresolved diagnostics | 159 | 34 (`proof`: 27, `web`: 7) | **0** (baseline file deleted or empty `{}`) |
