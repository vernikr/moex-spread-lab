# Automated Code Health & AI-Readability Plan

Status: **Proposal & Implementation Blueprint** (2026-11-21). No runtime code, dependencies, or CI workflows are modified in this turn.

## 1. Objective & Design Constraints

`moex-spread-lab` is developed primarily with AI coding agents across a hybrid **Python 3.12 + C99 (`_fastcore.c`) + vanilla HTML/JS (`proof_ui.html`)** codebase inside a `uv` workspace (`packages/*` + root compatibility layer). Automated code-health tooling must serve two concrete goals simultaneously:

1. **Prevent structural rot across fast agent iterations** — catch oversized files, high-nesting functions, missing type contracts, cross-layer leaks, silent C/Python drift, and copy-paste clones *before* merge.
2. **Maximize AI-agent accuracy and context efficiency** — ensure any module fits comfortably in a single agent read, function signatures expose complete contracts without opening 5 neighbor files, and hidden change coupling (`C <-> Python`, `strategy <-> proof <-> UI`) is surfaced mechanically rather than discovered through broken regressions.

### Non-Negotiable Repository Constraints

- **Private repository & strict data secrecy (`G12`):** Zero cloud SaaS code uploads; every tool must run 100% locally and inside GitHub Actions (`ubuntu-latest`, plus weekly `macos-latest` at 10x billing multiplier).
- **Zero production dependency bloat:** Runtime `dependencies` in `pyproject.toml` stay untouched. Fast deterministic Python gates belong in `[dependency-groups].dev` (locked in `uv.lock`); heavy or non-Python CLI tools run on-demand via `uvx` / `npx` or system `apt` in a dedicated CI job.
- **Bit-for-bit numerical & hash parity (`G1`):** Formatting, linting, or refactoring rules must never alter `run_id`, `config_hash` (`config/run-manifest.json`), C99 `-ffp-contract=off` floating-point semantics, or Stage-9 accounting contracts (`docs/development/execution-proof.md`).
- **Ratchet-first adoption (zero big-bang breakage):** Existing legacy hotspots are frozen behind an explicit baseline/ratchet so CI turns green on Day 1 while blocking any *new* violations or regressions on touched code.
- **Alignment with workspace migration:** Must complement, not duplicate, `tests/test_architecture_dag.py` and the Phases 2–6 modularization in [`docs/development/modular-architecture-plan.md`](modular-architecture-plan.md).

---

## 2. Empirical Baseline of This Repository (Measured 2026-11-21)

Before choosing tools, the current repository (`57` Python source files / `20,236` LOC, `1` C99 extension / `1,414` LOC, `1` single-file UI / `1,176` LOC, `120` code/test commits) was audited statically and via `git log`:

| Dimension | Measured baseline (2026-11-21) | Primary offenders | Impact on AI agents & maintenance |
|---|---|---|---|
| **Oversized files (>500 LOC)** | `13 / 57` Python files + `_fastcore.c` (`1,414` LOC) + `proof_ui.html` (`1,176` LOC) | `calendar_spreads.py` (`1,791`), `proof.py` (`1,586`), `_fastcore.c` (`1,414`), `proof_ui.html` (`1,176`), `cli.py` (`1,055`), `data_lake.py` (`965`), `proof_batch.py` (`892`) | High token cost per read; induces *lost-in-the-middle* attention decay and merge conflicts across parallel branches. |
| **Cognitive complexity (>15)** | `96 / 581` Python functions (`51` > 25, `19` > 50) | `cli.py:main` (`343`), `analytics.py:run_mean_reversion_baseline` (`178`), `position_sizing.py:size_proof` (`158`), `screener_app.py:make_handler` (`153`, nesting depth `9`), `proof.py:_mean_reversion_proof` (`148`), `carry.py:build_carry_strategy` (`143`) | Deeply nested branches (`depth 5–9`) cause fragile agent diffs and missed edge cases on fail-closed rules. |
| **C99 function size & PyObject lifecycle** | `6` C functions > 100 LOC in `_fastcore.c` | `fast_size_proof_metrics` (`378` LOC, manual `Py_INCREF`/`Py_DECREF` + `goto cleanup`), `fast_calendar_sample_z` (`149` LOC), `clean_recursive` (`124` LOC) | One missed `PyBuffer_Release` or `Py_DECREF` on an error branch leaks memory inside long-lived worker pools; C compiler currently runs only `-Wall` without static analyzer or sanitizers. |
| **Type annotation gaps** | `184 / 581` functions (`31.7%`) lack full parameter or return annotations; zero static type checker in CI | `cli.py`, `screener_app.py`, legacy `s3_reference.py`, `dict[str, Any]` payloads across proof boundaries | Agents must open 3–6 upstream/downstream files to infer dict keys and nullable fields instead of trusting signatures. |
| **Temporal (change) coupling in `git`** | Strong cross-layer co-change clusters across 120 commits | `position_sizing.py <-> proof.py` (`89%`), `proof_batch.py <-> proof_search.py` (`82%`), `proof_search.py <-> proof_ui.html` (`81%`), `proof.py <-> screener_app.py` (`65%`), plus dual-implementation parity between `_fastcore.c` and `analytics.py` / `calendar_spreads.py` / `position_sizing.py` / `s3_reference.py` | Agents editing one side of a coupled pair risk leaving the twin out of sync unless coupling is surfaced or eliminated by Phase 2–5 plugin boundaries. |
| **Cross-file duplication** | Low (`1` exact 12-line clone pair: `bar_types.py <-> data_lake.py`; intentional C/Python mirrors covered by `test_perf_equivalence.py`) | Transitional shims and `bar_types.py` extraction | Low today, but high risk during Phases 2–5 extraction if copy-paste is not gated. |
| **Existing guardrails already strong** | 6-layer AST DAG & zero-cycle check (`tests/test_architecture_dag.py`), bit-for-bit C/Python parity (`packages/proof/tests/test_perf_equivalence.py`), `cli secret-scan` | N/A | **Keep as authoritative** — do not replace working zero-dependency project gates with weaker generic tools. |

---

## 3. Conscious Tool Evaluation & Selection Matrix

Every candidate tool from the reference report was evaluated specifically against `moex-spread-lab`’s architecture, runtime, licensing, and CI cost profile:

### 3.1 Selected Stack (High Signal-to-Noise, Zero Redundancy)

| Layer / Concern | Selected Tool | Execution Mode | Why Selected for `moex-spread-lab` |
|---|---|---|---|
| **1. Fast Python lint, structural budgets, formatting & partial typing** | **`ruff`** (Astral, Rust, MIT) | Locked in `dev` group (`pyproject.toml`), runs in CI `<1 s` | Replaces Flake8/isort/Black/Pylint-refactor in one binary. Enforces `C901` (McCabe), `PLR0911..0915` (returns/branches/args/statements), `B` (bugbear), `BLE`/`TRY` (silent exception swallowing — critical for fail-closed `G6/G9` rules), `RUF`, `UP`, and `ANN` (public type annotations on refactored `packages/*`). |
| **2. Python Cognitive Complexity (Sonar model) with ratchet** | **`complexipy`** (Rust, MIT) | Locked in `dev` group (`pyproject.toml`), runs in CI `<1 s` | Unlike `C901`, penalizes **nesting depth** (`if` inside `for` inside `try`), which directly predicts where LLM edits break financial state machines. Supports JSON/SARIF output and per-file/baseline ratcheting. |
| **3. Deterministic Python static type checking** | **`basedpyright`** (MIT) | Locked in `dev` group (`pyproject.toml`), runs in CI `~3–5 s` | Understands `uv` workspaces + PEP 420 implicit namespace packages (`moex_spread_lab.*`) out of the box and supports per-directory strictness (`strict` on extracted `packages/*`, `standard`/ratchet on legacy `src/moex_spread_lab/*`). *(Note: Astral’s `ty` is fast and promising, but `basedpyright` currently has mature per-folder strictness overrides and baseline files `.basedpyright/baseline.json` essential for incremental onboarding without rewriting 20k LOC at once.)* |
| **4. Unified C99 + Python size & token-budget gate** | **`lizard`** (Python, MIT) | Locked in `dev` group (`pyproject.toml`), runs in CI `<2 s` | Single lightweight CLI covering **both** `packages/core/.../_fastcore.c` and `**/*.py` without needing full CMake. Directly measures per-function `NLOC`, `CCN`, parameter count, and **`token_count`** (LLM context consumption). |
| **5. C99 AST cognitive complexity, bugprone & Undefined Behavior checks** | **`clang-tidy` + Clang ASan/UBSan** | System LLVM in GitHub Actions `code-health` job + generated `compile_commands.json` | `_fastcore.c` is 1,414 LOC of raw CPython C-API (`PyBuffer`, `PyMem_Malloc`, IEEE-754 double-double math). `clang-tidy` (`readability-function-cognitive-complexity`, `bugprone-*`, `clang-analyzer-core.*`) + running `test_perf_equivalence.py` under `-fsanitize=address,undefined` catches reference/buffer leaks and UB that Python linters cannot see. |
| **6. Dead code elimination** | **`vulture`** (Python, MIT) + whitelist | Locked in `dev` group (`pyproject.toml`), runs in CI `<1 s` | During Phases 1–6 package moves, orphaned helpers and stale shims easily linger in `src/` and confuse agent `grep` searches. A small `vulture_whitelist.py` handles dynamic CLI/HTTP/plugin entry points cleanly. |
| **7. Behavioral analysis (Hotspots, Code Health, Change Coupling) + Agent MCP** | **`repowise`** (`uv tool install repowise`, AGPL-3.0, local-only) | **Workstation CLI + local MCP server** + weekly scheduled CI report artifact (not a per-push install bottleneck) | Best open-source CodeScene equivalent: combines git churn/hotspots, temporal coupling (`_fastcore.c <-> Python`, `proof <-> web`), tree-sitter call graphs (Python + C via `compile_commands.json`), and a **local MCP server** so agents query blast radius and coupling before editing. AGPL-3.0 is safe because it runs strictly as an external local developer tool, never linked into or distributed with `moex-spread-lab`. |
| **8. Copy-paste & context-window token audit** | **`jscpd`** (`npx jscpd`) + **repo-native budget script** (`scripts/code_health_gate.py`) | Weekly / pre-PR audit (`jscpd`); per-push `<0.5 s` (`code_health_gate.py`) | `jscpd` checks cross-language clones (`.py`, `.c`, `.js`/`.html`) without requiring Java. Meanwhile, a 90-line stdlib script `scripts/code_health_gate.py` enforces exact **file-LOC / token budgets** and **ratchets** in `<0.5 s` on every push without pulling Node.js into the fast Python CI path! |

### 3.2 Explicitly Rejected or Deferred Tools (Avoiding Bloat & Overlap)

| Candidate from Report | Decision | Rationale for Rejection / Deferral |
|---|---|---|
| **`import-linter` / `pydeps`** | **Reject (redundant)** | `tests/test_architecture_dag.py` already enforces a stricter, zero-dependency AST check across both `src/` and `packages/*`, including **function-level lazy imports** and Tarjan SCC == 1. Adding `import-linter` duplicates an already superior project-native gate. |
| **`radon` / `xenon`** | **Reject (superseded)** | Superseded by `ruff` (`C901` + `PLR`), `complexipy` (true cognitive complexity), and `lizard` (C + Python NLOC/CCN/tokens). |
| **PMD CPD** | **Reject (heavy runtime)** | Requires a JVM installation in CI/workstation; `jscpd` (via already-present Node/`npx`) covers Python, C, JS, and HTML with zero Java footprint. |
| **Include-What-You-Use (`iwyu`) & GNU `cflow`** | **Reject (overkill for 1 C file)** | `_fastcore.c` is a single self-contained C99 translation unit with only **5 standard headers** (`Python.h`, `math.h`, `stdint.h`, `stdlib.h`, `string.h`) and 24 static functions. Header-graph pruning (`iwyu`) and multi-TU call trees (`cflow`) add zero value unless `_fastcore.c` is split into multiple `.c`/`.h` units in the future. |
| **Code-Maat** | **Reject (heavy runtime, superseded)** | Requires Clojure/JVM + manual log pipelines; `repowise` natively computes git hotspots and temporal coupling in Python/Rust, and if custom C↔Python co-change rules are needed in CI, 30 lines of Python over `git log` do it with zero dependencies. |
| **`mypy`** | **Reject in favor of `basedpyright`** | Slower on monorepo namespace packages and lacks `basedpyright`’s built-in baseline file workflow (`.basedpyright/baseline.json`) for zero-noise incremental adoption. |
| **CodeScene Cloud SaaS** | **Reject (`G12` privacy rule)** | Private repository; external SaaS access to repo history is unnecessary. *(Optional developer convenience: the **CodeScene IDE extension** in VS Code / Cursor may be used purely offline/locally by the user as a visual gutter indicator.)* |
| **`repomix`** | **On-demand CLI only (`npx repomix`)** | Useful interactively when diagnosing prompt packing or top token-heavy files, but should **not** run on every CI push (pulls npm packages and scans the repo slower than a native Python LOC/byte budget check). |

---

## 4. Target Architecture of the Code-Health Pipeline

The setup is split into **three execution tiers** so developer velocity and CI billing stay fast while deep C/behavioral checks still run automatically:

```text
+-----------------------------------------------------------------------------------+
| TIER 1: Fast Deterministic Gate (< 8 s wall time — Local pre-push & Every CI PR)  |
|   Runs via `uv run` from locked `uv.lock` (zero network, zero Node/JVM):          |
|   1. `ruff check` + `ruff format --check`        (< 0.5 s)                        |
|   2. `basedpyright` (strict on packages/*, baseline ratchet on legacy src/*)      |
|   3. `complexipy` + `lizard` + `vulture`         (< 2.0 s)                        |
|   4. `scripts/code_health_gate.py`               (< 0.4 s)                        |
|      - File LOC / token ceiling ratchet (`config/code-health-baseline.json`)      |
|      - C <-> Python `_fastcore` parity touch-check                                |
|   5. Existing `tests/test_architecture_dag.py` + `cli validate-manifest`          |
|      + `cli secret-scan --tracked-only`          (< 1.0 s)                        |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| TIER 2: C99 Deep Safety Gate (~25 s — Triggered in CI when `*.c` or loader        |
|         changes, plus weekly schedule)                                            |
|   1. Generate `compile_commands.json` via `scripts/gen_compile_commands.py`       |
|   2. `clang-tidy` (`bugprone-*`, `clang-analyzer-*`, cognitive complexity)        |
|   3. Compile `_fastcore.c` with `-O1 -g -fsanitize=address,undefined`             |
|      -fno-omit-frame-pointer -ffp-contract=off and run `test_perf_equivalence.py` |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
| TIER 3: Behavioral, Clone & Context Telemetry (~40 s — Workstation MCP +          |
|         Weekly Scheduled GitHub Action Artifact)                                  |
|   1. `repowise` index + health/hotspot/coupling report + local MCP for agents     |
|   2. `npx jscpd` cross-file clone check (`< 3%` threshold across py/c/js/html)    |
|   3. Top-20 token-heaviest files & refactoring queue artifact (`reports/`/CI)     |
+-----------------------------------------------------------------------------------+
```

---

## 5. Quantitative Budgets & Ratchet Policy ("AI-Readability Contract")

To prevent immediate CI failure on the 13 existing >500 LOC files and 96 >15 cognitive-complexity functions while strictly stopping *any* further degradation, rules apply **two thresholds**:

1. **Greenfield / Touched-Package Standard** (enforced immediately on `packages/*` and any newly created file/function):
2. **Legacy Baseline Ratchet** (`config/code-health-baseline.json` + `.basedpyright/baseline.json`): existing legacy files in `src/moex_spread_lab/*` cannot grow in LOC or complexity beyond their recorded baseline; when a file is refactored or extracted into `packages/*` during Phases 2–5, its legacy allowance is deleted permanently.

| Metric | Target Budget (`packages/*` & new code) | Hard Ceiling (Legacy ratchet cap) | Enforcing Tool |
|---|---|---|---|
| **Python / JS / CSS file length** | `<= 400` LOC (`<= 3,500` LLM tokens) | Ratcheted to current LOC + `0%` growth on files `> 500` LOC | `scripts/code_health_gate.py` |
| **C99 source file / function length** | Function `<= 100` NLOC, parameters `<= 7` | Existing `fast_size_proof_metrics` frozen at `378` LOC until split into static helpers | `lizard` + `clang-tidy` |
| **Python Cognitive Complexity** | `<= 15` per function (warning at `12`) | Legacy functions `> 15` listed in baseline; any edit to them may not increase score | `complexipy` |
| **C99 Cognitive Complexity** | `<= 20` per function (`IgnoreMacros: true`) | Legacy `fast_size_proof_metrics` exempted via `NOLINTNEXTLINE` with ticket until helper split | `clang-tidy` |
| **Function signature structural limits** | Args `<= 7` (`PLR0913`), branches `<= 12` (`PLR0912`), statements `<= 50` (`PLR0915`), returns `<= 6` (`PLR0911`) | Enforced strictly on `packages/*`; per-file ignores on unmigrated `src/*` | `ruff` |
| **Type annotation completeness** | `100%` of public functions/methods/dataclasses in `packages/*` (`ANN` + `basedpyright` strict) | `basedpyright` baseline lock on `src/moex_spread_lab/*` (0 new untyped errors) | `ruff` + `basedpyright` |
| **Code duplication** | `< 2.5%` duplicated lines/tokens (min 12 lines / 80 tokens) | `< 3.0%` repository-wide (excluding thin transitional re-export shims `< 20` LOC) | `jscpd` |
| **Dead / unreachable code** | `0` unused functions, classes, imports, or variables (`>= 80%` confidence) | `0` (verified dynamic symbols explicitly listed in `vulture_whitelist.py`) | `ruff` (`F401`/`F841`) + `vulture` |
| **Module dependency graph** | Max SCC size `== 1` (zero cycles incl. lazy imports); strict 6-layer DAG | Hard fail (`0` exceptions) | `tests/test_architecture_dag.py` |

---

## 6. Concrete Configuration Blueprints

Below are the exact configurations to be added during implementation (none applied yet).

### 6.1 `pyproject.toml` Additions (`dev` group + Ruff, BasedPyright, Complexipy, Vulture)

Add to `[dependency-groups].dev`:
```toml
[dependency-groups]
dev = [
    "basedpyright>=1.28,<2",
    "complexipy>=4.0,<6",
    "lizard>=1.17,<2",
    "pytest>=8.3,<10",
    "pytest-subtests>=0.14,<1",
    "pytest-xdist>=3.6,<4",
    "ruff>=0.9,<1",
    "vulture>=2.14,<3",
]
```

Test scheduling follows the Phase 6 configuration: package-matrix CI jobs use `pytest-xdist --dist loadfile`; no shared test-duration registry is used.

Tool sections in root `pyproject.toml`:
```toml
[tool.ruff]
target-version = "py312"
line-length = 100
src = ["packages/core/src", "src", "tests"]
extend-exclude = ["src/moex_spread_lab/vendor"]

[tool.ruff.lint]
select = [
    "E", "W",   # pycodestyle
    "F",        # pyflakes (unused imports/vars)
    "I",        # deterministic import sorting (reduces agent merge conflicts)
    "B",        # flake8-bugbear
    "BLE",      # blind-except (guards fail-closed financial/risk invariants!)
    "UP",       # pyupgrade (modern Python 3.12 syntax: X | Y, type aliases)
    "SIM",      # flake8-simplify
    "C4",       # comprehensions
    "PIE",      # misc lints
    "C901",     # McCabe cyclomatic complexity ceiling
    "PLR0911",  # too-many-return-statements
    "PLR0912",  # too-many-branches
    "PLR0913",  # too-many-arguments
    "PLR0915",  # too-many-statements
    "RUF",      # Ruff-specific rules
]
ignore = [
    "E501",     # line length handled by formatter / semantic readability
]

[tool.ruff.lint.mccabe]
max-complexity = 15

[tool.ruff.lint.pylint]
max-args = 7
max-branches = 12
max-returns = 6
max-statements = 50

[tool.ruff.lint.per-file-ignores]
# Enforce strict public type annotations on modular workspace packages:
# (enabled via extend-select in packages/*/pyproject.toml or per-path override)
"tests/**/*.py" = ["PLR0913", "PLR0915", "BLE001"]
"packages/*/tests/**/*.py" = ["PLR0913", "PLR0915", "BLE001"]
# Temporary legacy structural exemptions removed as Phases 2-5 extract each file:
"src/moex_spread_lab/{cli,calendar_spreads,proof,proof_batch,analytics,carry,position_sizing,screener_app,pair_screener,data_refresh,s3_reference}.py" = [
    "C901", "PLR0911", "PLR0912", "PLR0913", "PLR0915",
]

[tool.basedpyright]
pythonVersion = "3.12"
include = ["packages", "src", "tests"]
exclude = ["src/moex_spread_lab/vendor", "**/__pycache__"]
extraPaths = ["packages/core/src", "src"]
baselineFile = ".basedpyright/baseline.json"
typeCheckingMode = "standard"
# Strict contract enforcement on all cleanly extracted workspace packages:
strict = ["packages/core/src"]
reportMissingTypeStubs = false

[tool.complexipy]
paths = ["packages", "src"]
max-complexity-allowed = 15
quiet = true

[tool.vulture]
paths = ["packages", "src", "scripts/vulture_whitelist.py"]
exclude = ["*/vendor/*"]
min_confidence = 80
sort_by_size = true
```

### 6.2 C99 Tooling: `compile_commands.json` Generator & `.clang-tidy`

Because `_fastcore.c` is compiled dynamically via `sysconfig.get_path("include")` in `_fastcore_loader.py` rather than CMake, `clang-tidy` and `repowise` need a deterministic `compile_commands.json` pointing to the active Python 3.12 headers.

1. **`scripts/gen_compile_commands.py`** (~35 LOC, stdlib-only):
   Queries `sysconfig.get_path("include")` and `shutil.which("clang") or shutil.which("cc")`, emitting `compile_commands.json` at the repo root (git-ignored) with exact production flags (`-std=c99 -O3 -ffp-contract=off -fPIC -Wall -Wextra -I<python_inc>`).
2. **`.clang-tidy`** at repository root:
   ```yaml
   Checks: >
     -*,
     bugprone-*,
     -bugprone-easily-swappable-parameters,
     clang-analyzer-core.*,
     clang-analyzer-unix.Malloc,
     clang-analyzer-security.*,
     performance-*,
     readability-function-cognitive-complexity,
     readability-function-size,
     readability-misleading-indentation
   WarningsAsErrors: >
     bugprone-use-after-move,
     clang-analyzer-core.*,
     clang-analyzer-unix.Malloc
   CheckOptions:
     - key: readability-function-cognitive-complexity.Threshold
       value: '20'
     - key: readability-function-cognitive-complexity.IgnoreMacros
       value: 'true'
     - key: readability-function-size.LineThreshold
       value: '120'
     - key: readability-function-size.ParameterThreshold
       value: '8'
   ```
3. **Compiler warning hardening in `_fastcore_loader.py` (when C health gate is enabled):**
   Add `MOEX_FASTCORE_SANITIZE=1` support in `_fastcore_loader.py` (test-only env flag, never active by default) that compiles a separate `_fastcore_asan<ext>` binary with `-O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined -ffp-contract=off` so CI can run `packages/proof/tests/test_perf_equivalence.py` under ASan/UBSan without touching the production `.so` cache.

### 6.3 Custom Repo-Native Gate: `scripts/code_health_gate.py`

A fast (`<0.4 s`), zero-dependency Python script executed in CI and locally that enforces rules generic linters miss:

1. **File LOC & Token Budget Ratchet (`config/code-health-baseline.json`):**
   - Scans `packages/**/*.{py,c,js,css,html}` and `src/**/*.{py,c,js,css,html}` (excluding `vendor/`).
   - Approximates token count (`utf8_bytes / 3.6` or exact `tiktoken` if installed) and counts non-blank LOC.
   - **Rule A:** Any file *not* in `config/code-health-baseline.json` fails if `LOC > 450` (Python/JS/HTML/CSS) or `LOC > 600` (C99).
   - **Rule B:** Any legacy file listed in `config/code-health-baseline.json` fails if its current `LOC` exceeds `baseline_loc` (strict non-increase ratchet) and suggests `--update-baseline` when `LOC` drops by `>= 10%` so wins are locked in automatically.
2. **Explicit C99 ↔ Python Fallback Coupling Check:**
   - Every function registered in `FastCoreMethods` inside `packages/core/src/moex_spread_lab/core/_fastcore.c` has a pure-Python twin (`compute_rolling_z`, `calendar_sample_z`, `calendar_zscore_scan`, `calendar_convergence_scan`, `clean_obj`, `mean_reversion_scan`, `size_proof_metrics`).
   - Checks that every exported method name in `FastCoreMethods` is referenced in `packages/proof/tests/test_perf_equivalence.py`, preventing an agent from adding a new C fast-path function without a bit-for-bit Python equivalence test!
3. **Blind `except Exception:` Guard on Financial/Accounting Layers:**
   - Verifies zero unannotated `except Exception:` blocks inside `packages/core` (except `_fastcore_loader.py` optional-build fallback), `packages/accounting`, and `packages/data/lake` (`h1_seal.py`), protecting `G6/G8/G9` fail-closed semantics.

### 6.4 Duplication Config: `.jscpd.json`

```json
{
  "threshold": 3.0,
  "minLines": 12,
  "minTokens": 85,
  "mode": "strict",
  "format": ["python", "c", "javascript", "markup"],
  "path": ["packages", "src"],
  "ignore": [
    "**/vendor/**",
    "**/__pycache__/**",
    "packages/**/tests/**",
    "tests/**"
  ],
  "reporters": ["console", "json"],
  "output": "reports/code-health/jscpd"
}
```
*(Note: `.gitignore` ignores `reports/code-health/` so ephemeral local diagnostics are never committed accidentally.)*

### 6.5 Agent Intelligence & Behavioral Layer: `repowise` + `AGENTS.md` Integration

1. **Workstation setup (one-time on developer macOS machine):**
   ```sh
   uv tool install repowise
   python3 scripts/gen_compile_commands.py
   repowise init .
   ```
   Configure `.repowise/` in `.gitignore` so local SQLite/graph indexes never pollute git status or trigger `G12` checks.
2. **MCP Server wiring for local/workstation coding agents (Claude Code / Cursor / Codex):**
   Expose `repowise mcp` read-only so before editing any file with high coupling or fan-in, the agent calls Repowise tools (`get_file_context`, `get_change_coupling`, `get_hotspots`, `find_dead_code`) instead of reading 6,000+ lines of raw source into context.
3. **Weekly Scheduled Health Audit (`.github/workflows/code-health-weekly.yml`):**
   Runs every Monday alongside scheduled CI: executes `repowise` health/coupling snapshot + `npx jscpd@4` clone report + `lizard` top-20 token-heavy functions, and uploads a single markdown summary (`code-health-digest.md`) to the GitHub Actions Job Summary and 14-day artifact (zero automatic commits to `main`).

---

## 7. Phased Rollout & Implementation Plan

Implementation is organized into **five small, non-disruptive PRs** aligned with `docs/development/modular-architecture-plan.md` so ongoing Stage-9 feature/migration work is never blocked.

### Step 1 — Deterministic Python Gate & Baseline Freeze (PR 1, ~1–2 hours)

**Scope:** `pyproject.toml`, `uv.lock`, `.basedpyright/baseline.json`, `config/code-health-baseline.json`, `scripts/code_health_gate.py`, `scripts/vulture_whitelist.py`.

1. Add `ruff`, `basedpyright`, `complexipy`, `lizard`, and `vulture` to `[dependency-groups].dev` and run `uv lock && uv sync --frozen`.
2. Configure `[tool.ruff]`, `[tool.basedpyright]`, `[tool.complexipy]`, and `[tool.vulture]` in `pyproject.toml`.
   - Fix safe zero-semantic lint issues (`F401` unused imports, `I001` import sorting) **only** in a dedicated style-only commit verified against `packages/proof/tests/test_perf_equivalence.py` and `config/run-manifest.json`. Do **not** run mass auto-rewrites across numerical expressions.
3. Generate initial `.basedpyright/baseline.json` for legacy `src/moex_spread_lab/*` while ensuring `packages/core/src` passes `strict` mode with **zero** errors (adding missing annotations to `packages/core` if any).
4. Implement `scripts/code_health_gate.py` and generate `config/code-health-baseline.json` recording exact current LOC and cognitive-complexity ceilings for existing legacy offenders (`>450` LOC files and `>15` complexity functions).
5. Add a single CLI convenience entrypoint or Makefile/script (`scripts/check_health.sh`) that runs Tier 1 end-to-end in `<8 s`.

**Acceptance Gate:**
- `scripts/check_health.sh` finishes in `<8 s` with exit code `0`.
- Introducing a synthetic 20-complexity function or growing `proof.py` by 10 LOC fails `check_health.sh` deterministically.
- `uv run pytest -q -n auto --dist loadfile` and `packages/proof/tests/test_perf_equivalence.py` pass 100%.

---

### Step 2 — C99 Static Analysis & Sanitizer Gate (PR 2, ~1–2 hours)

**Scope:** `scripts/gen_compile_commands.py`, `.clang-tidy`, `packages/core/src/moex_spread_lab/core/_fastcore_loader.py`, `packages/core/src/moex_spread_lab/core/_fastcore.c`, `.gitignore`.

1. Add `compile_commands.json` and `.repowise/` to `.gitignore`.
2. Implement `scripts/gen_compile_commands.py` and `.clang-tidy`.
3. Run `clang-tidy` on `_fastcore.c`:
   - Fix any genuine `clang-analyzer` / `bugprone` findings immediately.
   - Refactor `fast_size_proof_metrics` (`378` LOC) into 3–4 focused `static inline` C helpers (`parse_trade_specs`, `step_open_position`, `try_open_candidate`, `emit_ledger_row`) **or** add a scoped `NOLINTNEXTLINE(readability-function-size, readability-function-cognitive-complexity)` comment tracked for cleanup, verifying 0-ULP parity via `packages/proof/tests/test_perf_equivalence.py`.
4. Add optional `MOEX_FASTCORE_SANITIZE=1` compilation path in `_fastcore_loader.py` (separate `.so` suffix so production cache is untouched) and verify `MOEX_FASTCORE_SANITIZE=1 uv run pytest packages/proof/tests/test_perf_equivalence.py` passes with zero ASan/UBSan reports on Linux and macOS.

**Acceptance Gate:**
- `clang-tidy` reports `0` errors on `_fastcore.c`.
- `test_perf_equivalence.py` passes under AddressSanitizer + UndefinedBehaviorSanitizer (`ASAN_OPTIONS=detect_leaks=1`).

---

### Step 3 — CI Integration Without Slowing Shards (PR 3, ~1 hour)

**Scope:** `.github/workflows/ci.yml`, `.github/workflows/code-health-weekly.yml`, `docs/development/ci.md`.

1. Add a lightweight parallel job `code-health` to `.github/workflows/ci.yml` (`ubuntu-latest`, timeout 5 min):
   - Runs `uv sync --frozen`, then Tier 1 (`ruff check`, `basedpyright`, `complexipy` ratchet, `lizard`, `vulture`, `scripts/code_health_gate.py`, `tests/test_architecture_dag.py`, `cli validate-manifest`, `cli secret-scan --tracked-only`).
   - Runs in parallel with `test` shards `1/2`, `2/2`, and `perf-equivalence` — **adds `0 s` to critical-path CI wall time** (`~15–20 s` total job duration vs `~35 s` test shards).
   - Conditionally runs Tier 2 (`clang-tidy` + ASan/UBSan `test_perf_equivalence.py`) in the same workflow whenever `packages/core/**/_fastcore*` or `.clang-tidy` changes (using `dorny/paths-filter` or `git diff --name-only`).
   - Keep `.github/workflows/ci-macos.yml` free of extra linter runs to avoid 10x macOS billing waste.
2. Add `.github/workflows/code-health-weekly.yml` (Monday 04:30 UTC + `workflow_dispatch`):
   - Runs full Tier 2 (`clang-tidy` + ASan/UBSan unconditionally), `npx jscpd@4`, and `repowise` health/hotspot/coupling analysis.
   - Publishes a Markdown table of Top-10 Hotspots (lowest Code Health × highest recent churn), Top Change-Coupled file pairs, and Duplicate % directly into `$GITHUB_STEP_SUMMARY`.

**Acceptance Gate:**
- PR checks show `code-health` green in `<30 s` on `ubuntu-latest`.
- Weekly workflow dispatch produces readable `$GITHUB_STEP_SUMMARY` digest with zero repo dirtiness.

---

### Step 4 — Lock-Step Ratchet Burn-Down During Workspace Phases 2–5 (Ongoing)

Rather than scheduling risky standalone "refactoring sprints" on untested legacy code, tie baseline burn-down directly to the already planned workspace extractions in [`docs/development/modular-architecture-plan.md`](modular-architecture-plan.md):

| Workspace Migration Phase | Mandatory Code-Health Upgrade Executed in Same PR |
|---|---|
| **Phase 2 (`packages/data` & `packages/accounting`)** | Add `"packages/data/src"` and `"packages/accounting/src"` to `basedpyright` `strict` list + Ruff `ANN` enforcement; split `position_sizing.py:size_proof` (`398` LOC, `cog=158`) and `data_lake.py` (`965` LOC) below `400` LOC / `cog <= 15`; delete their entries from `config/code-health-baseline.json` and eliminate the `bar_types.py <-> data_lake.py` clone. |
| **Phase 3 (`packages/strategies/*`)** | Decompose `calendar_spreads.py` (`1,791` LOC), `analytics.py:run_mean_reversion_baseline` (`cog=178`), and `carry.py:build_carry_strategy` (`cog=143`) into isolated `StrategyPlugin` subpackages (`<= 350` LOC/file, `cog <= 15`, 100% typed `ParameterSpec`/`WindowSnapshot` dataclasses). |
| **Phase 4 (`packages/proof` & `cli.py`)** | Replace monolithic `cli.py:main` (`1,055` LOC, `cog=343`) with `<70` LOC entry-point loader; split `proof.py` (`1,586` LOC) and `proof_batch.py` (`892` LOC) into typed `service.py` / `windows.py` / `batch.py` modules (`strict` typing enabled). |
| **Phase 5 (`packages/web` & `proof_ui.html`)** | Decompose `proof_ui.html` (`1,176` LOC, #1 churn magnet with 50 commits) and `screener_app.py:make_handler` (`cog=153`, nesting depth `9`) into route modules (`<= 250` LOC) + native ES modules under `static/js/*.js` (`<= 300` LOC each). |

---

### Step 5 — Workstation MCP & Agent Operating Rules (PR 5, ~30 min)

**Scope:** `AGENTS.md` (1 concise bullet), workstation `repowise` setup note in `docs/development/ci.md`.

1. Install and index `repowise` on the primary workstation (`~/Downloads/projects/moex-spread-lab`) and register its local MCP server for workstation-connected coding agents.
2. Add one compact working rule to `AGENTS.md` once Tier 1 is live:
   - Before committing code changes, run `uv run scripts/check_health.sh`; never raise ceilings in `config/code-health-baseline.json` or add `# noqa` / `# type: ignore` without an inline justification, and always run `test_perf_equivalence.py` when touching `_fastcore.c` or its pure-Python mirrors.
3. Feed the weekly `code-health-digest.md` JSON/Markdown output directly to coding agents for targeted maintenance tasks (*"Pick hotspot #1 with `cog > 15`, decompose into pure typed helpers, keep `pytest` and `test_perf_equivalence.py` bit-for-bit green, and run `code_health_gate.py --update-baseline`"*).

---

## 8. Summary of Expected Impact

| Failure Mode Today | Automated Protection After Rollout |
|---|---|
| Agent grows an already huge file (`calendar_spreads.py`, `proof.py`, `proof_ui.html`) to 2,000+ LOC | **Blocked in `<0.4 s`** by `code_health_gate.py` LOC/token ratchet (`0%` growth allowed above cap; `400` LOC max for new files). |
| Agent writes a 7-level nested `if/for/try` patch that subtly breaks edge-case accounting | **Blocked in `<1 s`** by `complexipy` (`cog <= 15`) and `ruff` (`PLR0912` / `C901` / `BLE001`). |
| Agent adds untyped `dict[str, Any]` helper in `packages/*`, forcing future agents to read 5 files | **Blocked in `~3 s`** by `basedpyright` (`strict` on `packages/*`) + `ruff` (`ANN`). |
| Agent modifies `_fastcore.c` with a subtle `Py_DECREF` leak, buffer overrun, or missing Python fallback test | **Blocked in CI** by `clang-tidy` (`clang-analyzer-unix.Malloc`), ASan/UBSan `test_perf_equivalence.py`, and `FastCoreMethods` parity check. |
| Agent leaves dead functions/imports during package extraction or duplicates 30 lines across modules | **Caught automatically** by `vulture` + `ruff F401` (per push) and `jscpd` (`<3%` clone ceiling, weekly/pre-PR). |
| Agent misses hidden co-change dependency (`position_sizing <-> proof`, `proof_search <-> UI`) | **Eliminated structurally** by Phases 2–5 `StrategyPlugin` + surfaced pre-edit via local `repowise` MCP coupling queries. |
