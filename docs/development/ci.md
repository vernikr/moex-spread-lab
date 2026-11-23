# CI and fast test runs

Continuous integration runs on every push to `main`, on pull requests, weekly (Monday 04:17 UTC) and on manual dispatch (`.github/workflows/ci.yml`). It deploys nothing and never touches local market-data caches.

## Layout

- `scope` runs first (plan S2). On `ubuntu-latest` with system `python3`, no `uv sync`: `scripts/change_scope.py` routes the PR/push diff (or `git diff base..head`) and emits `docs_only`, the `packages` test-leg list, `has_packages`, `run_tier2` and `perf_equivalence`, plus a printed reason per decision. `schedule`, `workflow_dispatch`, a missing/null base SHA and any router or git error resolve to full (all six legs). Full logic lives in `scripts/change_scope.py` (stdlib only).
- `architecture-gate` (`ubuntu-latest`) always runs (`<2 s` script runtime). After syncing the locked workspace, `scripts/architecture_gate.py` checks the full package/import DAG, the no-legacy-shim rule for internal code/tests, built-in and synthetic strategy discovery, `validate-manifest`, and `secret-scan --tracked-only`.
- `code-health` (`ubuntu-latest`, 5-minute timeout) always runs, in parallel with the test jobs so it adds `0 s` to critical-path CI wall time:
  - Always runs Tier 1 (`./scripts/check_health.sh`: `ruff check`, `ruff format --check`, `basedpyright` strict/baselined typecheck, `vulture`, `scripts/code_health_gate.py` LOC/token/cognitive/C/parity/exception ratchets, and `scripts/architecture_gate.py`).
  - Conditionally runs Tier 2 (`scripts/gen_compile_commands.py` + `clang-tidy` + `scripts/run_sanitized_pytest.py` under ASan + UBSan + LSan) whenever `scripts/should_run_c_tier2.py` detects changes to `_fastcore.c`, `_fastcore_loader.py`, `.clang-tidy`, sanitizer/parity scripts/tests, or on `schedule` / `workflow_dispatch`. (`should_run_c_tier2.py` keeps its CLI and delegates the pattern match to `scripts/change_scope.py`; the `scope` job emits its own `run_tier2` for consumers that want one.)
- `package-tests` is a matrix over the legs selected by `scope` (`packages/<member>/tests` with `pytest -n auto --dist loadfile`, `--durations=15` artifact). Reverse-dependency closure: `core -> all six`, `data/accounting -> {self, strategies, proof, web}`, `strategies -> {self, proof, web}`, `proof -> {self, web}`, `web -> {web}` — the table lives in `scripts/change_scope.py` and is pinned to the architecture gate's layer DAG by `packages/core/tests/test_change_scope.py`. Docs-only changes run **zero** package legs (the matrix is skipped, `ci-ok` stays green). Non-`.py` package files and new/deleted files run full legs without testmon deselection; unmatched paths (`pyproject.toml`, `uv.lock`, `.github/`, `config/`, `fixtures/`, root `tests/`, `scripts/`, root `src/`, ...) resolve to all six legs.
- `perf-equivalence` runs `packages/proof/tests/test_perf_equivalence.py` separately so bit-for-bit numerical and hash parity remains a merge gate. It is skipped only for docs-only and web-only changes (scope decides), never otherwise.
- `ci-ok` is the final always-running job (`if: always()`). It fails when any needed job failed or was cancelled; skipped jobs are fine. It gives one stable required-check name for branch protection.
- Every job except `scope` and `ci-ok` uses `astral-sh/setup-uv`, caches by `uv.lock`, and runs `uv sync --frozen`. Test jobs also fail if the C99 `_fastcore` extension cannot compile (there is no silent Python fallback in CI).
- `concurrency` cancels superseded runs on the same ref.
- Routing rules and the docs-only definition (`docs/**` plus root-level `*.md`; Markdown inside `packages/**` is not docs) live in `scripts/change_scope.py`; `packages/core/tests/test_change_scope.py` covers docs-only detection, package scoping, non-`.py` fallback, unknown-path -> full, and table drift. Routing is advisory only: the weekly scheduled run stays full as a backstop for routing mistakes. Markdown under `packages/*/docs/` is a module guide, so editing it routes to that package's legs, not to the docs-only profile.
- A docs-only change runs `scripts/docs_scan.py` on the changed files (whitespace, conflict markers, credential-shaped tokens). Link integrity is a separate, dependency-free command: `uv run python scripts/docs_scan.py --repo --links` resolves every relative link and anchor in `README.md`, `AGENTS.md`, `docs/**` and `packages/*/docs/**`; it is mandatory before moving or renaming a document. Preserved evidence (`docs/inputs/`, `docs/research/results/received/`, `docs/research/reuse/`) is reproduced as received and is excluded from link checking so it is never rewritten.

Pytest's `testpaths` includes only the six package test directories. Root `tests/` contains shared synthetic fixture modules used by package and browser tests; it is importable through pytest's `pythonpath` but is not independently collected.

## Other workflows

- `.github/workflows/code-health-weekly.yml` — scheduled weekly (Monday 04:30 UTC) and on manual dispatch. Runs Tier 1 + unconditional Tier 2 (`clang-tidy` + ASan/UBSan), `npx jscpd@4.0.5 --config .jscpd.json` (`<3.0%` duplication gate), and `repowise` behavioral indexing (`--no-prose --follow-renames`), then renders Top-10 Hotspots, Top Change-Coupled file pairs, Duplicate %, and Top-20 token-heaviest files/functions into `$GITHUB_STEP_SUMMARY` and a 14-day `code-health-digest` artifact with zero working-tree dirtiness.
- `.github/workflows/ci-macos.yml` — opt-in (manual dispatch, weekly Tuesday). Runs the architecture gate and full package-scoped test suite on `macos-latest` to catch clang/path drift; extra linters stay out of this workflow because macOS minutes bill at a 10x multiplier on private repositories.
- `.github/workflows/browser-smoke.yml` — manual dispatch and weekly Wednesday. Runs the architecture gate, installs Playwright Chromium, then runs every synthetic-data script in `packages/web/tests/browser/*_smoke.py`. Browser startup remains out of per-push CI.

## Local runs

```sh
uv sync --frozen
./scripts/check_health.sh                    # full Tier 1 gate
uv run python scripts/verify.py --profile quick     # changed-files lint/type/ratchet only
uv run python scripts/verify.py --profile affected  # docs-scan or router-scoped tests (advisory)
uv run python scripts/verify.py --profile full      # full gate + full suite
uv run python scripts/change_scope.py               # show the routing decision for the working tree
uv run python scripts/change_scope.py --format json
uv run python scripts/gen_compile_commands.py
uv run python scripts/run_sanitized_pytest.py -v packages/proof/tests/test_perf_equivalence.py
uv run pytest packages/proof/tests -q
uv run pytest -q -n auto --dist loadfile
```

`verify.py` composes the S0/S1 pieces instead of replacing them; every step is wrapped by `scripts/verify_record.py` (plan S0 measurement records). The `affected` profile is advisory: it labels its pytest leg `affected (advisory)`, runs the architecture gate and the always-run set (subprocess/CLI/`proof_pool` tests, plus `test_perf_equivalence.py`/`test_fastcore_parity.py` when `core`/`proof`/`accounting` changed) with testmon disabled, and never substitutes for the full gate before a push. Use `quick` for inner-loop feedback and `full` before pushing.

Use a package-specific path for focused work (for example `packages/strategies/tests`). The full suite uses the six configured `testpaths`; no central duration file needs refreshing or committing. For the sequential reference path, run `uv run pytest -q`.

## Workstation `repowise` MCP setup

Workstation coding agents can query local structural/git-churn hotspots, change-coupled files, and blast radius without touching tracked repo files (`.repowise/` is gitignored):

```sh
uv tool install repowise   # on macOS x86_64 if tree-sitter-gdscript sdist lacks headers, pass CFLAGS="-I<tree-sitter-v0.22-src-include>"
REPOWISE_SKIP_EDITOR_SETUP=1 REPOWISE_NO_SAVE_KEY=1 \
  repowise init . --yes --no-prose --follow-renames \
  --no-editor-setup --no-hook --no-claude-md --no-agents --no-codex --no-onboarding
```

Register `repowise mcp ~/Downloads/projects/moex-spread-lab` (stdio MCP server; add `--tools core,+get_health` if desired) in user-scoped workstation agent configs (for example `claude mcp add --scope user moex-repowise -- repowise mcp ~/Downloads/projects/moex-spread-lab`, Codex `~/.codex/config.toml`, or user-level OpenCode config) so repository files stay clean. Refresh the index after refactors with `repowise update --index-only --no-agents`.

## Diagnostics

- `--durations=15` keeps slow tests visible in CI artifacts; the bit-for-bit equivalence suite remains isolated in its own job.
- The reference-equivalence tests honor `MOEX_PROOF_REFERENCE_EVAL=1` and `MOEX_PROOF_DISABLE_C=1`; use them to diagnose numeric drift, not as CI defaults.
- If xdist ever flakes, reproduce sequentially and file the ordering dependency as a bug; do not mask it by disabling `perf_equivalence`.
