# Modular Architecture & Multi-Agent Workspace Plan

Status: **Proposal & Migration Blueprint** (2026-11-21). No runtime code or directory moves are executed in this turn.

## 1. Executive Summary & Architectural Verdict

**Goal:** Restructure `moex-spread-lab` so multiple coding agents can work concurrently in isolated branches (`git worktree` / remote agent sandboxes) and merge back into `main` with near-zero textual or semantic conflicts.

### Assessment of the Proposed Approach

1. **Co-locating code, `tests/`, `docs/`, `fixtures/`, and `manifest.json` per bounded module — Strongly Recommended.**
   When Agent A implements a new strategy and Agent B modifies data-lake archival, co-location guarantees their diffs touch completely disjoint directory subtrees. Neither agent edits a shared `tests/` listing, a monolithic `cli.py`, or a central `README` index.
2. **Putting every module directly at the repository root (`/core`, `/data`, `/strategies`, …) — Suboptimal; use a single `/packages/` container instead.**
   In Python repositories, root-level domain folders collide with repository infrastructure (`config/`, `docs/`, `fixtures/`, `reports/`, `data/`, `data-lake/`, `.github/`) and confuse build backends, `pytest` rootdir discovery, and `.gitignore` rules. The Python packaging standard (**`uv` workspaces**, PEP 621 multi-package monorepo — the exact Python analog of Rust Cargo `crates/*` or TypeScript `packages/*`) places all self-contained modules under `packages/<module>/` while keeping project-wide governance, Stage 1–8 records, and the root `uv.lock` at the repository root.
3. **Subfolders per concrete strategy (`strategies/pair_mean_reversion/`, `strategies/funding_carry/`, …) — Strongly Recommended, with plugin auto-discovery.**
   Subfolders eliminate conflicts *only* if adding or editing a strategy does not require touching central `if/elif` dispatchers. Today, adding one strategy requires editing 7 shared files (`screener_app.py`, `proof.py`, `proof_batch.py`, `proof_strategy_search.py`, `proof_signals.py`, `calendar_spreads.py`, `proof_ui.html`). Pairing strategy subfolders with a declarative `StrategyPlugin` protocol + `importlib.metadata` entry points reduces cross-branch touchpoints to **zero shared files**.

---

## 2. Diagnosis of Current Coupling & Merge Hotspots

Static AST import analysis of `src/moex_spread_lab/` (45 files, ~20.4k LOC) reveals why parallel agent branches currently collide:

### 2.1 Cyclic Dependency Clusters (Strongly Connected Components)

Today the flat `src/moex_spread_lab` namespace contains **11 mutual 2-file cycles** and **two large strongly connected components (SCCs)**:

- **SCC-1 (Data & H1 Seal, 3 files):** `data_lake` ↔ `h1_seal` ↔ `h1_migration`.
- **SCC-2 (Proof, Sizing, Strategies & HTTP UI, 12 files):**
  `calendar_spreads`, `position_sizing`, `proof`, `proof_batch`, `proof_funding`, `proof_pool`, `proof_search`, `proof_strategy_search`, `proof_windows`, `s3_reference`, `screener_app`, `screener_search`.

 Concrete inversions causing SCC-2:
- Domain engines (`calendar_spreads.py`, `s3_reference.py`, `proof.py`, `proof_funding.py`) import constants and data loaders (`INITIAL_CAPITAL`, `MIN_DAILY_VOLUME`, `HISTORY_WINDOWS`, `PAIRS`, `pair_price_inputs`) upward from the HTTP server module `screener_app.py`.
- `position_sizing.py` imports `_cost_totals` and `_run_id` back from `proof.py`.
- `proof.py` imports `CohortWorkerPool` from `proof_pool.py` while `proof_pool.py` imports `proof.py`.

### 2.2 Six Shared Merge-Conflict Magnets

| Hotspot file | LOC | Why parallel branches collide on it |
|---|---:|---|
| `src/moex_spread_lab/cli.py` | 1,055 | Single `build_parser()` and `main()` importing 26 internal modules; every new command edits both top imports and the dispatch table. |
| `src/moex_spread_lab/proof.py` | 1,580 | Combines catalog assembly, pair/calendar adapters, ledger accounting, benchmark marking, and payload hashing in one file. |
| `src/moex_spread_lab/calendar_spreads.py` | 1,545 | Mixes MOEX contract chains/specs, ISS downloads, H1 liquidity gates, RUONIA convergence, and Z-score mean reversion. |
| `src/moex_spread_lab/proof_ui.html` | 1,176 | Inline CSS + DOM + hardcoded strategy forms (`#pair-controls`, `#calendar-controls`, `STRATEGY_RU`) in one single-file HTML asset. |
| `src/moex_spread_lab/screener_app.py` | 1,018 | Mixes pair universe registry, candle normalization, legacy S2 routes, `/api/proof/*` routes, settings persistence, and HTTP server lifecycle. |
| `.github/test-durations.json` | ~180 | Committed flat timing file touched whenever tests are added, renamed, or reweighted across branches. |

---

## 3. Target Architecture: Conventional `uv` Workspace + PEP 420 Namespace

### 3.1 Core Design Decisions

1. **`uv` Workspace Monorepo (`packages/*`):**
   Each bounded domain is a first-class workspace member with its own `pyproject.toml`, `src/`, `tests/`, `docs/`, `fixtures/`, and `manifest.json`, unified by **one** root `uv.lock` and **one** shared `.venv`.
2. **PEP 420 Implicit Namespace Package (`moex_spread_lab.<domain>`):**
   Every package exposes `src/moex_spread_lab/<domain>/...` (with **no** `src/moex_spread_lab/__init__.py` inside individual workspace members). Clean domain imports (`from moex_spread_lab.core.w0 import ...`, `from moex_spread_lab.strategies.funding_carry import ...`) work seamlessly across editable installs without colliding.
3. **Compatibility Facade Root Package (`moex-spread-lab`):**
   The root umbrella package depends on all workspace members, preserves `uv run python -m moex_spread_lab.cli ...`, and keeps thin re-export shims during migration so existing scripts, Stage-9 run manifests, and external callers never break abruptly.
4. **Strict Acyclic Layering (DAG) Enforced in CI:**
   Dependencies flow strictly downward (`Layer 0 -> Layer 1 -> Layer 2 -> Layer 3 -> Layer 4`). Upward and lateral cross-strategy imports are mechanically blocked by `import-linter` (or an AST DAG test with zero external dependencies).
5. **Plugin Registries via Python `entry_points` (`importlib.metadata`):**
   - CLI subcommands register under `[project.entry-points."moex_spread_lab.cli"]`.
   - Strategy implementations register under `[project.entry-points."moex_spread_lab.strategies"]`.
   - UI strategy parameter controls render dynamically from each strategy's JSON parameter schema instead of hardcoded HTML `<div id="pair-controls">` blocks.

### 3.2 Target Repository Directory Layout

```text
/home/user/webapp/
├── pyproject.toml                  # Root umbrella package + [tool.uv.workspace] members = ["packages/*"]
├── uv.lock                         # Single locked environment for all workspace packages
├── AGENTS.md                       # Global working rules & module boundary rules
├── README.md                       # High-level overview & links
├── config/                         # Frozen run manifests & benchmark suites (run-manifest.json)
├── docs/                           # Cross-cutting governance ONLY:
│   ├── decisions.md                #   User decision register (A-01..A-14, BR-10, etc.)
│   ├── process.md                  #   Stage gates
│   ├── blockers/                   #   b01, b02
│   ├── inputs/                     #   Brief, redacted server spec, tariff PDFs
│   ├── risk/                       #   Provisional pilot risk policy
│   ├── stages/                     #   01-unpacking .. 08-planning (historical stage records)
│   ├── research/                   #   Standalone R01-R15 prompts & received reports
│   └── development/                #   Cross-cutting architecture, CI & migration guides
├── fixtures/                       # Frozen cross-wave replay fixtures (w0, w1-w7)
├── reports/                        # Committed baseline replay outputs
│
├── packages/                       # === MODULAR WORKSPACE (1 folder = 1 bounded context) ===
│   │
│   ├── core/                       # [Layer 0] Kernel, hashing, fastcore C99, domain contracts/protocols
│   │   ├── pyproject.toml          #   name = "msl-core"
│   │   ├── manifest.json           #   Stage-5 module contract manifest
│   │   ├── docs/
│   │   ├── fixtures/
│   │   ├── src/moex_spread_lab/core/
│   │   │   ├── __init__.py
│   │   │   ├── w0.py               #   Canonical JSON, SHA-256, HashedJSONCache, secret scanner
│   │   │   ├── timeframes.py       #   TimeframeSpec registry (1d, 1h)
│   │   │   ├── settings.py         #   ResearchSettings, EvaluationSplit, global constants (MIN_DAILY_VOLUME, INITIAL_CAPITAL)
│   │   │   ├── contracts.py        #   Typed dataclasses/Protocols: StrategyPlugin, BarSeries, CostProfile, ProofTarget
│   │   │   ├── _fastcore.c         #   C99 hot-loop accelerator
│   │   │   └── _fastcore_loader.py #   Atomic clang builder & pure-Python fallback loader
│   │   └── tests/
│   │       ├── test_w0.py
│   │       └── test_evaluation_periods.py
│   │
│   ├── data/                       # [Layer 1] Market data intake, Parquet lake, H1 seal, universes & benchmark
│   │   ├── pyproject.toml          #   name = "msl-data" (depends: msl-core)
│   │   ├── manifest.json
│   │   ├── docs/                   #   data-lifecycle.md, storage-policy, historical-pair-intake.md
│   │   ├── fixtures/
│   │   ├── src/moex_spread_lab/data/
│   │   │   ├── __init__.py
│   │   │   ├── cli.py              #   Entry-point CLI plugin: data-*, iss-*, benchmark-*, storage-*
│   │   │   ├── iss/                #   Submodule: MOEX ISS HTTP client, W1 catalog, EOD history fetch
│   │   │   ├── lake/               #   Submodule: MarketDataStore, ParquetBarStore, data_refresh, h1_seal, h1_migration
│   │   │   ├── retention/          #   Submodule: data_inventory, data_retention, data_archive_candidates, h1_archive, storage_policy
│   │   │   ├── universe/           #   Submodule: pair registry (PAIRS, ASSETS, contract chains, historical-contracts)
│   │   │   └── benchmark/          #   Submodule: LQDT/VTBM/RUSFAR/RUONIA lineage & mark lookup
│   │   └── tests/
│   │       ├── test_iss.py
│   │       ├── test_data_lake.py
│   │       ├── test_h1_seal.py
│   │       ├── test_benchmark.py
│   │       └── ...
│   │
│   ├── accounting/                 # [Layer 1] Tariffs, VTB account risk, causal sizing, matched LQDT ledger
│   │   ├── pyproject.toml          #   name = "msl-accounting" (depends: msl-core)
│   │   ├── manifest.json
│   │   ├── docs/                   #   execution-ledger.md, vtb-account-risk-profile.md
│   │   ├── src/moex_spread_lab/accounting/
│   │   │   ├── __init__.py
│   │   │   ├── cli.py              #   Entry-point CLI plugin: vtb-risk-check, ledger/artifact commands
│   │   │   ├── costs.py            #   MOEX taker tariff + slippage profiles + cost_totals reconciliation
│   │   │   ├── account_risk.py     #   VTB EBS/KPUR margin & stress-buffer gates
│   │   │   ├── portfolio.py        #   Matched LQDT wealth overlay V(t), B(t), X(t), H(t), D(t)
│   │   │   ├── position_sizing.py  #   Causal max_affordable whole-contract sizing
│   │   │   ├── ledger.py           #   W4/W5 append-only execution ledger
│   │   │   └── artifacts.py        #   run_artifacts, run_artifact_import, validation
│   │   └── tests/
│   │       ├── test_position_sizing.py
│   │       ├── test_execution_accounting.py
│   │       ├── test_run_artifacts.py
│   │       └── ...
│   │
│   ├── strategies/                 # [Layer 2] Pluggable strategy families (1 subfolder = 1 isolated strategy)
│   │   ├── pyproject.toml          #   name = "msl-strategies" (depends: msl-core, msl-data, msl-accounting)
│   │   ├── manifest.json
│   │   ├── docs/
│   │   ├── src/moex_spread_lab/strategies/
│   │   │   ├── __init__.py         #   Plugin discovery registry via entry_points / local subpackages
│   │   │   ├── base.py             #   StrategySpec, ParameterField, SignalSeries, EntryBand builders
│   │   │   ├── pair_mean_reversion/#   Submodule: ordinary pair rolling Z-score MR + signed funding integration
│   │   │   │   ├── __init__.py
│   │   │   │   ├── plugin.py       #     Declares schema, compatibility, signals, entry bands
│   │   │   │   ├── engine.py       #     Core analytics & rolling Z
│   │   │   │   └── docs/
│   │   │   ├── funding_carry/      #   Submodule: H2/H3 perpetual SWAPRATE carry & midpoint resolver
│   │   │   │   ├── __init__.py
│   │   │   │   ├── plugin.py
│   │   │   │   ├── engine.py       #     carry.py + proof_funding.py + funding_recheck.py
│   │   │   │   └── docs/
│   │   │   ├── calendar_zscore/    #   Submodule: native & H1-proxy calendar Z-score mean reversion
│   │   │   │   ├── __init__.py
│   │   │   │   ├── plugin.py
│   │   │   │   └── engine.py
│   │   │   ├── calendar_ruonia/    #   Submodule: implied rate convergence to RUONIA + dividend adjustments
│   │   │   │   ├── __init__.py
│   │   │   │   ├── plugin.py
│   │   │   │   └── engine.py
│   │   │   └── s3_reference/       #   Submodule: audited S3 SBRF historical reference reproduction
│   │   │       ├── __init__.py
│   │   │       └── engine.py
│   │   └── tests/
│   │       ├── contract/           #   Shared conformance suite run automatically against EVERY registered strategy
│   │       │   └── test_strategy_conformance.py
│   │       ├── pair_mean_reversion/
│   │       ├── funding_carry/
│   │       ├── calendar_zscore/
│   │       └── calendar_ruonia/
│   │
│   ├── proof/                      # [Layer 3] Execution-proof engine, Optuna cohort search, plateau & worker pool
│   │   ├── pyproject.toml          #   name = "msl-proof" (depends: msl-core, msl-data, msl-accounting, msl-strategies)
│   │   ├── manifest.json
│   │   ├── docs/                   #   execution-proof.md, performance-optimization.md
│   │   ├── src/moex_spread_lab/proof/
│   │   │   ├── __init__.py
│   │   │   ├── cli.py              #   Entry-point CLI plugin: proof-*, audit, pair-screen, calendar-study
│   │   │   ├── service.py          #   ProofService orchestration & payload assembly (execution-proof.v1)
│   │   │   ├── windows.py          #   StudyWindow & read-only window_summaries
│   │   │   ├── batch.py            #   Fixed-cohort evaluator & preflight compatibility
│   │   │   ├── search.py           #   Single-family Optuna TPE + 1D neighbor plateau lamps
│   │   │   ├── strategy_search.py  #   Cross-family common-strategy selector
│   │   │   ├── pool.py             #   Sticky-sharded spawn worker pool
│   │   │   ├── legacy_s2.py        #   S1/S2 compatibility table & grid search (screener.py, screener_search.py)
│   │   │   └── audit.py            #   Independent P&L recomputation audit
│   │   └── tests/
│   │       ├── fixtures.py
│   │       ├── test_execution_proof.py
│   │       ├── test_proof_batch.py
│   │       ├── test_proof_search.py
│   │       ├── test_strategy_search.py
│   │       └── test_perf_equivalence.py
│   │
│   └── web/                        # [Layer 4] Loopback HTTP API, static UI assets (ES modules) & browser smoke suites
│       ├── pyproject.toml          #   name = "msl-web" (depends: msl-core, msl-proof)
│       ├── manifest.json
│       ├── docs/                   #   screener-s1.md, screener-s2.md, remote-agent-testing.md
│       ├── src/moex_spread_lab/web/
│       │   ├── __init__.py
│       │   ├── cli.py              #   Entry-point CLI plugin: `screener` server command
│       │   ├── server.py           #   ThreadingHTTPServer, loopback Host/Origin guards, lock registry
│       │   ├── routes_proof.py     #   /api/proof/* JSON & NDJSON streaming handlers
│       │   ├── routes_settings.py  #   /api/settings, /api/timeframes, /api/strategies
│       │   ├── routes_legacy.py    #   /api/screen, /api/run, /api/optimize (S2 compatibility)
│       │   ├── static/             #   Modular frontend assets (zero build step, native ES modules!)
│       │   │   ├── index.html      #     Minimal structural markup (<180 lines)
│       │   │   ├── css/
│       │   │   │   ├── layout.css
│       │   │   │   ├── picker.css
│       │   │   │   ├── charts.css
│       │   │   │   └── ledger.css
│       │   │   └── js/
│       │   │       ├── app.js
│       │   │       ├── state.js
│       │   │       ├── api.js
│       │   │       ├── picker.js
│       │   │       ├── strategy-form.js  # Schema-driven parameter inputs + plateau lamps
│       │   │       ├── charts.js
│       │   │       ├── ledger.js
│       │   │       └── settings-modal.js
│       │   └── vendor/             #   Same-origin Lightweight Charts 5.3.0 + Apache-2.0 license
│       └── tests/
│           ├── test_http_routes.py
│           └── browser/            #   Playwright Chromium smoke suites (*_smoke.py)
│
└── src/moex_spread_lab/            # Thin root umbrella & backward-compatibility shim layer
    ├── cli.py                      # Discovers `moex_spread_lab.cli` entry points (<70 LOC)
    └── ...                         # Deprecated re-export shims during transition window
```

---

## 4. Strict Dependency DAG & Anti-Conflict Mechanics

### 4.1 Allowed Import Graph (Strictly Acyclic)

```text
                    +-------------------+
                    |      msl-web      |  Layer 4: HTTP loopback & ES-module UI
                    +---------+---------+
                              |
                              v
                    +-------------------+
                    |     msl-proof     |  Layer 3: Cohort replay, Optuna search, worker pool
                    +---------+---------+
                              |
                              v
                    +-------------------+
                    |  msl-strategies   |  Layer 2: Independent strategy plugins (isolated subdirs)
                    +----+---------+----+
                         |         |
              +----------+         +----------+
              v                               v
    +-------------------+           +-------------------+
    |     msl-data      |           |  msl-accounting   |  Layer 1: Market lake/ISS vs Risk/Ledger/Sizing
    +---------+---------+           +---------+---------+
              |                               |
              +---------------+---------------+
                              v
                    +-------------------+
                    |     msl-core      |  Layer 0: W0 hashing, settings, fastcore C99, Protocols
                    +-------------------+
```

**Hard Architectural Rules (enforced automatically in CI):**
1. **`msl-core` imports nothing** outside Python stdlib (plus clang runtime build for `_fastcore.c`).
2. **`msl-data` and `msl-accounting` are siblings** — neither imports the other. Both depend only on `msl-core`.
3. **Strategy isolation rule:** Inside `packages/strategies/src/moex_spread_lab/strategies/<strategy_a>/`, importing `<strategy_b>` is forbidden. Shared math helpers belong in `strategies/base.py` or `msl-core`.
4. **Future Basket / Live Modules:** When Stage-9 **S4 (Shared-Capital Basket)** or gated **Execution / Notification adapters** are built later, they drop cleanly into `packages/basket/` (Layer 3) and `packages/execution/` or `packages/notifications/` without modifying existing strategy or accounting packages.

### 4.2 Eliminating Central Registration Bottlenecks

#### A. Strategy Plugin Contract (`StrategyPlugin`)
Instead of `if strategy == "funding_only": ... elif strategy == "zscore_mean_reversion": ...` scattered across 7 files, every subfolder in `packages/strategies/` exports one declarative plugin object:

```python
@dataclass(frozen=True)
class ParameterSpec:
    name: str
    label_ru: str
    kind: Literal["int", "float"]
    default: int | float
    min_value: int | float
    max_value: int | float
    step: int | float
    tunable: bool = True
    lamp_steps: tuple[int, ...] = (-2, -1, 1, 2)

class StrategyPlugin(Protocol):
    strategy_id: str                       # e.g. "funding_only"
    family_id: str                         # e.g. "mean_reversion" (unifies pair & calendar MR adapters!)
    label_ru: str
    supported_universes: frozenset[str]    # {"pair_screen"} or {"calendar_native"}
    supported_fills: tuple[str, ...]       # ("next_open", "next_close", "reference_same_bar") or ("same_day_settlement",)
    requires_daily_bars: bool

    def parameter_schema(self, context: InstrumentContext) -> tuple[ParameterSpec, ...]: ...
    def validate_parameters(self, raw: Mapping[str, Any], context: InstrumentContext) -> dict[str, Any]: ...
    def check_compatibility(self, window: WindowSnapshot, settings: ResearchSettings) -> CompatibilityVerdict: ...
    def evaluate_intents(self, window: WindowSnapshot, params: Mapping[str, Any], fill: str) -> StrategyIntentRun: ...
    def compute_entry_bands(self, series: list[dict], params: Mapping[str, Any], fill: str) -> list[EntryBandPoint]: ...
```

**Why this makes parallel agent work conflict-free:**
- Agent A creates `packages/strategies/src/moex_spread_lab/strategies/kalman_spread/plugin.py` and registers it in `packages/strategies/pyproject.toml` (or auto-discovers `*/plugin.py` via `pkgutil.iter_modules` — **zero** central file edits!).
- `msl-proof` discovers the new strategy automatically for `/api/proof/run`, `/api/proof/optimize`, `/api/proof/plateau`, and `/api/proof/windows`.
- `msl-web` serves `/api/strategies` from the plugin's `parameter_schema()`, and `strategy-form.js` renders the controls and stability lamps automatically without editing HTML.

#### B. CLI Subcommand Discovery (`cli.py` -> Entry Points)
Root `cli.py` shrinks from 1,055 lines to ~60 lines:
- Scans `importlib.metadata.entry_points(group="moex_spread_lab.cli")` sorted deterministically by command name.
- Each package (`msl-data`, `msl-accounting`, `msl-proof`, `msl-web`) registers only its own subcommands in its own `cli.py`.
- Two agents adding CLI commands in different packages never touch the same file.

#### C. Modular Static UI Without Adding Node/Bundler Complexity
`proof_ui.html` (1,176 lines) is split into native browser ES modules (`<script type="module" src="/static/js/app.js">`) and split CSSstylesheets served same-origin by `msl-web`:
- Preserves the **zero Node.js / zero bundler / offline same-origin** constraint (Lightweight Charts 5.3.0 stays vendored at `/vendor/lightweight-charts.standalone.production.js`).
- An agent fixing chart tooltips edits `static/js/charts.js`; an agent improving the period table edits `static/js/picker.js`; an agent styling the settings modal edits `static/css/layout.css` + `static/js/settings-modal.js`. Merge conflicts in UI work drop by >85%.

#### D. Conflict-Free CI Test Sharding
- Replace the monolithic `.github/test-durations.json` with per-package test execution (`uv run pytest packages/<pkg>/tests`) on PRs, plus full workspace verification (`uv run pytest`) where `pytest` discovers `testpaths = ["packages/*/tests", "tests"]`.
- If duration-based splitting is kept, generate or partition durations per package (`packages/<pkg>/.test-durations.json`) so adding tests in `strategies` never conflicts with test duration updates in `data`.

---

## 5. Multi-Agent Parallel Workflow Rules (Post-Migration)

Once this structure is in place, parallel agent work follows four mechanical rules:

1. **One Task = One Package Scope (Default):**
   Task prompts assign explicit package ownership (e.g., *"Scope: `packages/strategies/src/moex_spread_lab/strategies/funding_carry/` and its `tests/` only"*).
2. ** Isolated `git worktree` per Agent:**
   Each agent works in its own branch/worktree sharing read-only `data/` and `data-lake/` symlinks (aligned with `docs/development/remote-agent-testing.md`), running fast local checks in <3 seconds via `uv run --package msl-strategies pytest packages/strategies/tests`.
3. **Contract-First Cross-Module Changes:**
   If a feature genuinely spans layers (e.g., adding a new field to `WindowSnapshot` in `msl-core` consumed by `msl-strategies` and `msl-web`), merge the tiny, additive `msl-core` contract PR **first**, then fan out parallel implementation branches to `msl-data`, `msl-strategies`, and `msl-web`.
4. **Automated Boundary Gate in CI:**
   CI runs `dag-check` + `conformance-check` + `secret-scan` before `pytest`. Any accidental upward import or cross-strategy import fails in <1 second with a clear architectural error.

---

## 6. File-by-File Mapping (Current -> Target)

| Current path (`src/moex_spread_lab/...`) | Target workspace location (`packages/...`) | Notes on decoupling |
|---|---|---|
| `w0.py` | `packages/core/.../core/w0.py` | Absorbs `load_contracts` & `_run_id` (breaks `account_risk`/`carry` -> `analytics` and `position_sizing` -> `proof`). |
| `timeframes.py` | `packages/core/.../core/timeframes.py` | Unchanged; zero internal dependencies. |
| `evaluation_periods.py` | `packages/core/.../core/settings.py` | Absorbs `INITIAL_CAPITAL`, `MIN_DAILY_VOLUME`, and `MAX_QUANTITY`. |
| `universe.py` | `packages/core/.../core/universe.py` | Shared pair/universe registries (`PAIRS`, `SCREEN_PAIRS`, `EXTRA_PAIRS`, `HISTORY_WINDOWS`, `STRATEGIES`, `PAIR_UNIVERSE`, `CALENDAR_UNIVERSE`); zero external dependencies. |
| `_fastcore.c`, `_fastcore_loader.py` | `packages/core/.../core/_fastcore*` | Precompiled and verified by `msl-core` build/test step. |
| *(future Phase 3)* | `packages/core/.../core/contracts.py` | `StrategyPlugin`, `ParameterSpec` protocol contracts. |
| `iss.py` | `packages/data/.../data/iss/catalog.py` | Hosts shared `fetch_history`, `_history_url`, `HISTORY_SCHEMA`, and `IssHistoryError` (extracted from `carry.py`). |
| `bar_types.py`, `data_lake.py`, `data_refresh.py`, `h1_migration.py`, `h1_seal.py` | `packages/data/.../data/lake/` | `bar_types.py` holds `BarSeries`, `DataLakeError`, `SealedH1Reader`, and seal read/write guards so `bar_types` -> `data_lake` -> `h1_migration` -> `h1_seal` is strictly acyclic. |
| `data_inventory.py`, `data_retention.py`, `data_archive_candidates.py`, `h1_archive.py`, `storage_policy.py` | `packages/data/.../data/retention/` | Pure data-lifecycle & read-only storage budget operations (`storage_policy.py` inspects run-artifact manifests directly so `msl-data` never imports `msl-accounting`). |
| `benchmark.py` | `packages/data/.../data/benchmark/lineage.py` | Also provides `_benchmark_marks` lookup currently duplicated in `proof.py` / `carry.py`. |
| `costs.py` | `packages/accounting/.../accounting/costs.py` | Absorbs `_cost_totals` from `proof.py`. |
| `account_risk.py` | `packages/accounting/.../accounting/account_risk.py` | Uses `msl-core` (`w0.load_contracts`) instead of importing `analytics.py`. |
| `portfolio.py` | `packages/accounting/.../accounting/portfolio.py` | Pure matched-LQDT overlay math. |
| `position_sizing.py` | `packages/accounting/.../accounting/position_sizing.py` | Depends only on `msl-core` + `costs` + `account_risk` (zero dependency on `proof`). |
| `ledger.py`, `validation.py`, `run_artifacts.py`, `run_artifact_import.py`, `research.py` | `packages/accounting/.../accounting/` | W4/W5 execution ledger, W7 chronological validation, Parquet run-artifact storage & legacy importer, and W1–W7 offline research waves (`run_artifacts` -> `ledger`/`validation` -> `run_artifact_import` strictly acyclic). |
| `analytics.py`, `proof_signals.py` (pair MR part) | `packages/strategies/.../strategies/` | Strategy evaluation & entry-band math; wrapped in `StrategyPlugin` in Phase 3. |
| `carry.py`, `proof_funding.py`, `funding_recheck.py`, `funding_screen.py` | `packages/strategies/.../strategies/funding_carry/` | Wrapped in `StrategyPlugin`; raw ISS history fetcher moved down to `msl-data` (`iss.py`). |
| `calendar_spreads.py` (Z-score MR & RUONIA parts) | `packages/strategies/.../strategies/calendar_*` | Split into `calendar_zscore/` and `calendar_ruonia/` `StrategyPlugin` subpackages in Phase 3. |
| `s3_reference.py` | `packages/strategies/.../strategies/s3_reference/` | Audited reference backtest module. |
| `screener.py`, `pair_screener.py` | `packages/strategies/.../strategies/pair_screener/` | Offline pair-screener domain logic (`ScreenerInputs`, `pair_price_inputs`, `catalog`, `build_result`) extracted from `screener_app.py`. |
| `proof.py`, `proof_kernel.py`, `proof_pool.py`, `proof_windows.py`, `proof_batch.py`, `proof_strategy_search.py`, `proof_search.py` | `packages/proof/.../proof/` | `proof_kernel.py` isolates `StudyWindow`, `Landscape`, `_tune`, `_measure` so `proof` -> `proof_kernel` -> `proof_pool` -> `proof_windows`/`proof_batch` -> `proof_strategy_search` -> `proof_search` has zero intra-layer cycles. |
| `screener_search.py`, `audit.py` | `packages/proof/.../proof/` | S1/S2 compatibility evaluation and independent P&L audit. |
| `screener_app.py` (HTTP server & routes only) | `packages/web/.../web/` | Thin HTTP transport layer over `msl-proof`, `pair_screener`, and `msl-core` (retaining backward-compatible re-exports until Phase 6). |
| `proof_ui.html`, `vendor/*` | `packages/web/.../web/static/` & `vendor/` | Split into modular HTML/CSS/ES-modules; same-origin vendored Lightweight Charts preserved. |
| `cli.py` | `src/moex_spread_lab/cli.py` + `packages/*/cli.py` | Entry-point dispatcher + domain CLI plugins. |

---

## 7. Step-by-Step Migration Plan & Execution Instructions

### Invariants Across Every Phase (Non-Negotiable)

1. **Bit-for-bit numerical and hash parity:** `packages/proof/tests/test_perf_equivalence.py` and all functional tests must pass after *every single phase* without altering `run_id`, `config_hash` (`config/run-manifest.json`), reconciliation tolerances, or Stage-9 accounting semantics (`docs/development/execution-proof.md`).
2. **One phase = one small, reviewable PR/commit series:** Never combine code refactoring/logic changes with `git mv` bulk file relocations in the same commit (preserving `git log --follow` blame history).
3. **Backward-compatible CLI & HTTP contracts:** `uv run python -m moex_spread_lab.cli ...` and all `/api/*` endpoints remain 100% compatible throughout and after migration.
4. **No live/broker scope creep:** Do not alter risk limits (`docs/risk/pilot-policy.md`), research screen defaults, or loopback-only network rules.

---

### Phase 0 — Cycle Breaking & Constant Extraction (In-Place Preparation) — **Completed**

**Objective:** Eliminate all 11 mutual import cycles and cross-layer upward imports *before* moving any files, so every module already obeys a strict module-level and layer-level DAG (max SCC size = 1, including lazy imports) while still living in `src/moex_spread_lab/`.

**Completed Implementation:**
1. Extracted shared constants (`INITIAL_CAPITAL`, `MIN_DAILY_VOLUME`, `MAX_QUANTITY`) into `evaluation_periods.py` and pure universe registries (`PAIRS`, `SCREEN_PAIRS`, `EXTRA_PAIRS`, `HISTORICAL_PERPETUAL_PAIRS`, `HISTORY_WINDOWS`, `STRATEGIES`, `PAIR_UNIVERSE`, `CALENDAR_UNIVERSE`) into `universe.py` (L0 `core`), plus `load_contracts` and `_run_id` into `w0.py` (L0 `core`).
2. Extracted offline pair-screener domain logic (`ScreenerInputs`, `ScreenerError`, `_session_volumes`, `_positive_leg_volume_dates`, `pair_price_inputs`, `catalog`, `build_result`) from `screener_app.py` (L4 `web`) into `pair_screener.py` (L2 `strategies`), keeping backward-compatible re-exports and mock seams in `screener_app.py`.
3. Moved `_cost_totals` into `costs.py` (L1b `accounting`) and `_run_id` into `w0.py` so `position_sizing.py` no longer imports `proof.py`; kept `run_artifacts.py` in L1a `data` (`w0` + `pyarrow` storage) so `storage_policy.py` stays strictly inside L1a `data`.
4. Moved generic ISS EOD history pagination (`fetch_history`, `_history_url`, `HISTORY_SCHEMA`, `IssHistoryError`) from `carry.py` into `iss.py` (L1a `data`, with `CarryError(IssHistoryError)` preserved in `carry.py`) so `calendar_spreads.py` and `proof_funding.py` depend on `iss.py` rather than `carry.py`/`screener_app.py`.
5. Decoupled `data_lake.py` ↔ `h1_seal.py` ↔ `h1_migration.py` via `bar_types.py` (L1a `data`, hosting `BarSeries`, `DataLakeError`, `SealedH1Reader`, and seal table guards) and decoupled the L3 `proof_*` SCC via `proof_kernel.py` (`StudyWindow`, `Landscape`, `_tune`, `_measure`) + `proof_pool.get_worker_pool`, achieving **zero** cycles across all 47 modules.
6. Added `tests/test_architecture_dag.py` enforcing explicit layer membership for every module in `src/moex_spread_lab/`, zero import cycles (max SCC size = 1 including function-level imports), and strict 6-layer DAG boundaries (`core` -> `data` | `accounting` -> `strategies` -> `proof` -> `web` -> `cli`).

**Verification Gate:**
```sh
uv run pytest -q -n auto --dist loadfile
```

---

### Phase 1 — Bootstrap `uv` Workspace & Extract Layer 0 (`packages/core`) — **Completed**

**Objective:** Activate `[tool.uv.workspace]` and move `core` primitives (`msl-core`) with backward-compatible re-export shims.

**Completed Implementation:**
1. Created `packages/core/pyproject.toml` (`name = "msl-core"`, Setuptools PEP 420 namespace package under `src/moex_spread_lab/core`) and `packages/core/manifest.json`.
2. Activated `[tool.uv.workspace]` (`members = ["packages/*"]`) and `msl-core = { workspace = true }` in root `pyproject.toml`, enabled `pkgutil.extend_path` in `src/moex_spread_lab/__init__.py` so `moex_spread_lab.*` spans workspace distributions cleanly, and updated `uv.lock` with zero external dependency version drift.
3. Moved `w0.py`, `timeframes.py`, `evaluation_periods.py` (`settings.py` + `evaluation_periods.py` alias), `universe.py`, `_fastcore.c`, and `_fastcore_loader.py` into `packages/core/src/moex_spread_lab/core/` via `git mv`.
4. Added backward-compatible re-export shims in `src/moex_spread_lab/{w0,timeframes,evaluation_periods,universe,_fastcore_loader}.py` so every existing `moex_spread_lab.*` import continues to work unchanged.
5. Moved `tests/test_w0.py` and `tests/test_evaluation_periods.py` into `packages/core/tests/` (`testpaths = ["packages/core/tests", "tests"]`).
6. Updated `.github/workflows/ci.yml` and `.github/workflows/ci-macos.yml` C99 fastcore precompile checks to `moex_spread_lab.core._fastcore_loader`, and extended `tests/test_architecture_dag.py` to scan `packages/*/src/moex_spread_lab` and forbid extracted packages from importing umbrella shims.

**Verification Gate:**
```sh
uv sync --frozen
uv run python -c "from moex_spread_lab.core._fastcore_loader import load_fastcore; assert load_fastcore() is not None"
uv run pytest -q -n auto --dist loadfile
```

---

### Phase 2 — Extract Layer 1 Siblings (`packages/data` and `packages/accounting`) — **Completed**

**Objective:** Separate market data storage/intake from financial accounting/risk/sizing into independent parallel packages.

**Completed Implementation:**
1. Created `packages/data` (`msl-data`, depending on `msl-core`, `duckdb`, `pyarrow`, `zstandard`, `openpyxl`, `certifi`) with `packages/data/manifest.json` and moved ISS intake (`data/iss/catalog.py`), Parquet lake & H1 seal (`data/lake/{bar_types,data_lake,data_refresh,h1_migration,h1_seal}.py`), retention/archival (`data/retention/{data_inventory,data_retention,data_archive_candidates,h1_archive,storage_policy}.py`), `data/benchmark/lineage.py`, and `data/universe/__init__.py` (re-exporting `msl-core` universe definitions).
2. Decoupled `storage_policy.py` read-only TTL reporting (`_dry_run_gc_run_artifacts`) from `run_artifacts.py` (verified bit-for-bit against `gc_run_artifacts(..., dry_run=True)` in `test_storage_policy.py`) so `msl-data` and `msl-accounting` have **zero** cross-sibling imports.
3. Co-located 12 data test suites into `packages/data/tests/` and 9 data lifecycle/intake/lineage docs into `packages/data/docs/`.
4. Created `packages/accounting` (`msl-accounting`, depending only on `msl-core` and `pyarrow`) with `packages/accounting/manifest.json` and moved `costs.py`, `account_risk.py`, `portfolio.py`, `position_sizing.py`, `ledger.py`, `run_artifacts.py`, `run_artifact_import.py`, `validation.py`, and `research.py` into `packages/accounting/src/moex_spread_lab/accounting/`.
5. Co-located 5 accounting/sizing/ledger/artifact/validation test suites into `packages/accounting/tests/` and 6 accounting/risk/validation docs into `packages/accounting/docs/` (updating cross-references in `README.md` and `docs/`, while keeping `account_risk.py` runtime `working_profile` provenance string unchanged).
6. Added backward-compatible shims in `src/moex_spread_lab/` (including `sys.modules` identity aliasing where tests patch module-level symbols) and extended `tests/test_architecture_dag.py` to enforce strict sibling isolation (`msl-data` ⊥ `msl-accounting`) and forbid umbrella imports from `core`, `data`, and `accounting`.

**Verification Gate:**
```sh
uv sync --frozen
uv run pytest packages/core/tests packages/data/tests packages/accounting/tests -q
uv run pytest -q -n auto --dist loadfile
```

---

### Phase 3 — Extract Layer 2 (`packages/strategies`) with Strategy Subfolders & Plugin Protocol — **Completed**

**Objective:** Isolate every trading strategy into its own self-contained subfolder with conformance tests so multiple agents can build strategies concurrently with zero shared file edits.

**Completed Work:**
1. Defined `StrategyPlugin` (`@runtime_checkable` Protocol), `ParameterSpec`, `InstrumentContext`, `CompatibilityVerdict`, `EntryBandPoint`, `WindowSnapshot`, and `StrategyIntentRun` in `packages/core/src/moex_spread_lab/core/contracts.py`, plus `StrategyRegistry` and `discover_strategies()` in `packages/strategies/src/moex_spread_lab/strategies/base.py`.
2. Created `packages/strategies` (`msl-strategies`) with isolated strategy plugin subfolders (`{__init__,engine,plugin}.py` with zero cross-subfolder imports enforced by `test_architecture_dag.py`):
   - `strategies/pair_mean_reversion/` (`analytics.py`, `engine.py`, `plugin.py` — `mean_reversion`).
   - `strategies/funding_carry/` (`carry.py`, `proof_funding.py`, `funding_recheck.py`, `funding_screen.py`, `engine.py`, `plugin.py` — `funding_only`).
   - `strategies/calendar_zscore/` (`engine.py`, `plugin.py` — `zscore_mean_reversion`).
   - `strategies/calendar_ruonia/` (`engine.py`, `plugin.py` — `convergence_to_ruonia`).
   - Plus non-plugin Layer 2 dataset/reference modules: `strategies/s3_reference/`, `strategies/calendar/calendar_spreads.py`, `strategies/pair_screener/{screener,pair_screener}.py`, and plugin-dispatched `strategies/signals.py`.
3. Implemented auto-discovery in `moex_spread_lab.strategies.discover_strategies()` using `pkgutil.iter_modules` + `[project.entry-points."moex_spread_lab.strategies"]`, wired `proof_kernel.STRATEGY_AXES` and `signals.attach_entry_levels` to the registry, updated Layer 3–5 callers to import `msl-strategies` directly, and added backward-compatible shims in `src/moex_spread_lab/`.
4. Added `packages/core/tests/test_contracts.py` and `packages/strategies/tests/contract/test_strategy_conformance.py` — verifying auto-discovery, synthetic drop-in plugin folders with zero core edits, determinism, truncation causality (zero lookahead on entry bands and closed intents), fail-closed compatibility/parameter checks, and `test_architecture_dag.py` subfolder isolation.

**Verification Gate:**
```sh
uv run pytest packages/strategies/tests -q
uv run pytest packages/proof/tests/test_perf_equivalence.py -q
```

---

### Phase 4 — Extract Layer 3 (`packages/proof`) & CLI Plugin Dispatch

**Objective:** Move the proof orchestrator, cohort Optuna optimizer, plateau profiler, and worker pool into `packages/proof`, driven purely by the `StrategyPlugin` registry, and replace the monolithic `cli.py` with entry-point discovery.

**In progress (2026-11-21):** The proof runtime and its tests are extracted into `packages/proof` as `msl-proof`. The Python namespace is `moex_spread_lab.proof_runtime` to preserve the existing `moex_spread_lab.proof` module and monkey-patch identity through compatibility aliases. The screener dataset itself remains in `msl-strategies`, where Phase 3 placed it. CLI entry-point discovery is available for additional commands, but built-in CLI commands remain in the umbrella CLI and strategy search is not yet fully registry-driven. A provider registered under `moex_spread_lab.cli` is a callable accepting the `argparse` subparsers object; it must register its commands and set `_msl_cli_handler` to a callable accepting the parsed namespace and returning an exit code. Providers load in entry-point name order, duplicate commands are rejected by argparse, and `W0Error` retains the existing exit code 2. Installed providers execute trusted code; this is not a sandbox. The top-level `--help` formatting error caused by an unescaped percent sign was fixed. The remaining Phase 4 instructions and verification gate are not yet complete.

**Instructions:**
1. Create `packages/proof` (`msl-proof`) and `git mv` `proof.py` (`service.py`), `proof_windows.py`, `proof_batch.py`, `proof_search.py`, `proof_strategy_search.py`, `proof_pool.py`, `screener.py`, `screener_search.py`, and `audit.py`.
2. Refactor `strategy_search.py` and `batch.py` to query `discover_strategies()` instead of hardcoded strategy tuples, while preserving exact family ordering, seed derivation, trial counts (`TRIALS_PER_STRATEGY = 80`), and `run_id` payload canonicalization.
3. Split `src/moex_spread_lab/cli.py` into package-local `cli.py` files (`msl-data`, `msl-accounting`, `msl-proof`, `msl-web`) registered via `[project.entry-points."moex_spread_lab.cli"]`. Keep `moex_spread_lab.cli:main` as the thin 60-line loader so every existing CLI command (`screener`, `calendar-study`, `replay`, `secret-scan`, `data-refresh`, etc.) retains identical flags and exit codes.
4. Move proof & optimization tests (including `test_perf_equivalence.py`) to `packages/proof/tests/`.

**Verification Gate:**
```sh
uv run python -m moex_spread_lab.cli --help
uv run python -m moex_spread_lab.cli validate-manifest --manifest config/run-manifest.json
uv run python -m moex_spread_lab.cli secret-scan --tracked-only
uv run pytest -q -n auto --dist loadfile
```

---

### Phase 5 — Extract Layer 4 (`packages/web`) & Modularize Frontend Assets

**Objective:** Isolate the loopback HTTP server, decompose `proof_ui.html` into clean ES modules/CSS, and drive strategy form controls from `/api/strategies` schema metadata.

**Instructions:**
1. Create `packages/web` (`msl-web`) and split `screener_app.py` HTTP handlers into `server.py`, `routes_proof.py`, `routes_settings.py`, and `routes_legacy.py`.
2. Add `GET /api/strategies` returning registered `StrategyPlugin` schemas, labels, supported universes, and supported fills.
3. Split `proof_ui.html` into `static/index.html`, `static/css/*.css`, and `static/js/*.js` (native ES modules, served same-origin with strict `Content-Type` and loopback `Host`/`Origin` checks). Keep `/vendor/lightweight-charts.standalone.production.js` and its license notice intact.
4. Update `static/js/strategy-form.js` to render parameter inputs and plateau lamps dynamically from `/api/strategies` while preserving existing DOM `id` / `data-*` / `aria-*` attributes used by the 10 Playwright smoke suites.
5. Move `test_screener_app.py` and `tests/browser/*_smoke.py` into `packages/web/tests/`.

**Verification Gate:**
```sh
uv run pytest packages/web/tests/test_http_routes.py -q
# Run all 10 Playwright Chromium smoke suites:
for f in packages/web/tests/browser/*_smoke.py; do
  uv run --with playwright python "$f"
done
```

---

### Phase 6 — Cleanup Shims, CI Path-Aware Optimization & Governance Update

**Objective:** Finalize workspace hygiene, update CI workflows, and document agent ownership rules.

**Instructions:**
1. Migrate remaining internal imports off the root `src/moex_spread_lab/*.py` shims; retain only public entry points (`cli.py`, `__init__.py`) or explicitly documented compatibility aliases.
2. Update `.github/workflows/ci.yml`, `ci-macos.yml`, and `browser-smoke.yml`:
   - Add a fast `architecture-gate` step (<2 s): verifies acyclic package DAG, strategy conformance discovery, `validate-manifest`, and `secret-scan --tracked-only`.
   - Configure `pytest` `testpaths = ["packages/core/tests", "packages/data/tests", "packages/accounting/tests", "packages/strategies/tests", "packages/proof/tests", "packages/web/tests"]` and per-package duration files (or dynamic xdist loadfile scheduling without a single conflict-prone JSON).
3. Update `AGENTS.md`, `README.md`, and `docs/stages/05-architecture.md` with the new package map and per-package agent scoping rules.

**Final Acceptance Checklist:**
- [ ] Zero cyclic imports across the entire repository (`test_architecture_dag.py` green).
- [ ] Adding a dummy strategy folder in `packages/strategies/.../dummy_strategy/` makes it appear in `/api/strategies`, Optuna search, and UI controls **with zero edits to any existing tracked file**.
- [ ] `uv run pytest -q -n auto --dist loadfile` passes 100% of unit, integration, and `test_perf_equivalence.py` bit-for-bit checks.
- [ ] All 10 Playwright smoke tests pass headless.
- [ ] `config/run-manifest.json` and `reports/w0/replay.json` hashes match pre-migration values byte-for-byte.
