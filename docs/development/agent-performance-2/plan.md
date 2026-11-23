# Agent task-latency plan, generation 2: consolidated

**Status:** S0–S1 implemented 2026-11-23 (measurement wrapper `scripts/verify_record.py`, quiet/CPU-capped/change-aware `check_health.sh` with filesystem layer classification; see [baseline-2026-11-23.md](baseline-2026-11-23.md)). S2 onwards remain proposed. Originally Proposed, 2026-11-23. Consolidated from the two independent reviews of the same three agent exports (`plan1`, `plan2`), reconciled against `origin/main` at `370a95e` (code unchanged since `e12bb51`). This plan supersedes both source reviews, which are deleted in the same change. It extends the [first-generation plan](../agent-performance-plan.md) and does **not** authorize weaker gates, unattended deployment, or changes to the proof/accounting/research contracts. Scope is agent **task completion time**, not only screener runtime. **S2 was rewritten on 2026-11-23** after a tooling spike on this repository (see S2 evidence) and reconciled with the landed S0/S1 scripts; other slices change only in cross-references.

---

## 1. Consolidated view

### 1.1 What both reviews agreed on

Five points were established independently by both reviews from the same evidence. They are the load-bearing part of this plan.

1. **Redundant whole-workspace verification is the dominant cost.** Full `pytest` (~58% of summed shell time) plus the full health gate (~24%) accounted for roughly 80% of all active execution time across the three sessions, and both were repeatedly re-run on trees whose relevant inputs had not changed.
2. **Slice granularity, not tool speed, causes most of the loss.** Twelve ~100-LOC extractions each paid the complete repository-wide pre-push ceremony plus a workstation restart, and each ended the assistant turn, forcing a human "продолжи".
3. **Workstation restart and catalog cost is paid unconditionally.** `remote sync` runs `uv sync --frozen` and `launchd kickstart -k` even when only documentation or tests changed; `remote bench` physically copies `data/` and `data-lake/` on every candidate.
4. **The heavy equivalence and browser paths are structurally expensive.** `test_perf_equivalence.py` runs inside every default discovery; browser smokes start a cold Chromium, a fresh server and full-trial Optuna searches per scenario.
5. **Context bloat is a first-order problem.** ~47% of all tool text came from `Bash` `sed`/`cat`/`grep` reads (973 KB) and ~98% of edits bypassed structured edit tools, which is also what produced the fragile `str.replace`/`s.index` failures.

### 1.2 Where the reviews diverge, and the resolution adopted

| Question | `plan1` position | `plan2` position | Resolution in this plan |
| --- | --- | --- | --- |
| Metrics and savings | Exact per-call attribution; specific speedups ("8x", "3x–6x", "166.5 min -> 29–36 min") | Refuses attribution because exports lack timestamps, overlap and human idle time; publishes **acceptance targets**, not savings | Use `plan2`'s measurement discipline. `plan1` numbers are kept only as **diagnostic weights** (relative shares of summed call durations) and are explicitly not forecasts. |
| Publication cadence | Change `AGENTS.md` now: local commits per slice, full gate + push + one workstation sync per turn/phase | Cadence change only by explicit agreement; verification reuse must be proven by shadow runs first | Adopt `plan1`'s per-turn cadence as the **default operating rule**, but land it through an agreed `AGENTS.md` edit in S3, with `plan2`'s equivalence rule ("reuse only for the same check and unchanged relevant inputs") as the hard constraint. |
| Diff-aware test selection | Trust it immediately (`verify_slice.py` as the authoritative local command) | Shadow it next to the unchanged full gate for >=30 candidates before trusting it for early feedback | Do not hand-build a selector. S2 adopts off-the-shelf `pytest-testmon` (method-level) as an **advisory** inner-loop tool plus a small path router; static import-graph selection was measured on this repo and rejected as too coarse (S2 evidence). Shadow it in S3 and only then promote it. A passing subset is never reported as full verification. |
| `test_perf_equivalence.py` | Split into 3 parallel files + mark heavy tests `slow_equiv`, keep 1 canary in default runs | Keep representative full-resolution cases; never trim trial counts in a parity test; move only pure invariants | Split by **trial-count tiers**, not by deleting cases: canary (small trials) in default runs, full-resolution parity in `--pre-push` and CI. Preserve test-ID coverage and confirm canonical outputs before/after. |
| Browser smokes | Shared Chromium + `MOEX_UI_SMOKE_TRIALS=12`, target <35 s | Profile per-scenario first; move non-UI assertions to HTTP/JS contract tests while keeping the real UI runner as reference | Do both, in that order: shared-context runner and bounded trials first, then move assertions that carry no UI-specific behaviour. |
| `remote bench` data | APFS `cp -cR` copy-on-write + reuse the root `.venv`, target 2–4 s | Zero-copy read-only data access (per the original `remote-agent-testing.md` design), keyed caches, no shared writable state | `cp -cR` with `shutil.copytree` fallback is the implementation; a **mount/symlink read-only** variant is the preferred fallback on non-APFS filesystems. Never share a writable cache across trust boundaries. |
| `basedpyright` / PEP 420 | Build a `.typecheck_typings` symlink farm and turn `reportMissingImports` back on | Fix the resolution properly, then onboard strictness incrementally, one leaf-module group at a time | Adopt both, but **incremental onboarding is the gate**: `strict` expands only module group by module group, each with a passing gate. No bulk flip of 3,675 errors. |
| Ownership of gates in CI | Not addressed | Measure redundancy first; only then single-owner each gate, after confirming equivalent flags and collection parity | Keep CI untouched until S2 measurement; any de-duplication requires parity evidence in the same PR. |
| `F2` (dual proof pipeline) | Part of the plan (`AP2-P7`), "halves dual-path edits" | Out of scope: it is a correctness-sensitive architectural change, not a latency fix | Out of scope here. Referenced from [`phase7-backlog.md`](../phase7-backlog.md) as a separate, contract-guarded deliverable that this plan only makes cheaper to execute. |

### 1.3 What is deliberately excluded

- Any change to application behaviour, proof search, accounting, hashes or `run_id` parity.
- Any relaxation of `config/code-health-baseline.json` or `.basedpyright/baseline.json` ceilings, and any new `# noqa` / `# type: ignore` / broad `except`.
- Automatic restart of the workstation service on verified `main`, ambient credentials in prompts or Actions artifacts, and any hosted CI/queue product beyond what the repository already runs.
- The three raw exports themselves: they are analysed, not tracked, and the credentials they contain must be revoked by the owner (see §7).

---

## 2. Measurement contract (do this before optimizing)

Every later slice is judged against these rules. They exist because both reviews drew opposite conclusions from the same exports.

**Counting rules.** One `##### Used Bash` record = one call. Normalize `ms`/`s` and sum the *displayed, rounded* tool durations; a block's duration belongs to the block, not to each command inside it. For pytest, prefer the completion lines over the tool duration, and exclude focused runs from suite statistics. These are not wall times, not CPU times, and not a before/after benchmark: the three sessions ran on different revisions, machines and CPU quotas.

**Required record per verification run** (versioned, sanitized, no paths or secrets): task class, phase, base SHA, candidate input fingerprint, selected checks and the reason each was selected, runner/CPU quota, cold/warm status, queue/setup/collection/call/teardown durations where applicable, outcome (`passed` / `failed` / `timeout` / `not run` / `unavailable`), retries, publication and promotion status, phase start and end.

**Reuse rule.** A result may be reused only for the same check on unchanged relevant inputs; key the tree, lockfile, toolchain, config and execution mode. Dependencies and compiled artifacts may be cached; **failures may never be cached as passes**.

**Guardrails (must not regress).** Full-suite runs per delivery, human continuations, false skips, flakes, missed required checks, numerical or hash drift.

**Decision rule.** Adopt an optimization only if the end-to-end metric improves on matched revisions: task accepted -> first actionable check -> verified exact `main` SHA -> workstation health (when requested). A faster command that does not move that metric is not a win. Use >=5 paired runs per workload for diagnosis and >=20 comparable observations per cohort before acting on p90; publish sample counts and ranges.

---

## 3. Dependency-aware execution order

```text
S0  Measurement + policy (no behaviour change)         ~0.5 day
 └─> S1  Cheap full gate: quiet, capped, auto-classified ~1 day
      └─> S2  Test routing: testmon + path router + CI scope ~1.5 days
           └─> S3  Cadence change (AGENTS.md) + shadow   ~1 day
                ├─> S4a  Suite cost reduction            ~2–3 days
                ├─> S4b  Browser smoke cost reduction    ~1.5 days
                └─> S4c  Workstation sync/bench/catalog  ~2.5 days
                     └─> S5  Typing and inspection tooling ~2–3 days
                          └─> S6  Warm lane (only if still justified)
```

S4a/S4b/S4c are independent and may proceed in parallel; S5 does not block S4. S6 is explicitly conditional: skip it if S1–S4 already move the end-to-end metric enough, or if cold-lane queue time stays immaterial.

---

## 4. Slices

Each slice lists: problem (evidence), work, acceptance target, and the risk it carries.

### S0 — Measure the verification loop, pin the environment

**Problem.** Verification cost is currently anecdotal, and the same numbers are quoted as both causal evidence and as a forecast.

**Work.**
1. Emit the record of §2 from a thin wrapper around the existing commands (`check_health.sh`, the pytest invocations, `remote sync`/`bench`, the browser runner). Store records outside the tracked tree or in a gitignored directory; keep raw logs out of chat.
2. Pin the environment once (`uv sync --frozen`, then `uv run --no-sync`), enforce `MOEX_OFFLINE_ONLY=1` on synthetic paths, and record the worker count explicitly in every record.
3. Publish a sanitized baseline for the three replay workloads: P1 frontend/static fix, P7.1 cross-package plugin contract change, P7.2 multi-slice proof refactor.

**Acceptance.** Three baselines published with sample counts and ranges; every later slice cites them; no number in this plan that lacks a baseline row is treated as measured.

**Risk.** Low. Instrumentation only. The one real hazard is leaking account data, home paths or exports into a record; the sanitizer is therefore part of the acceptance criteria.

### S1 — Make the full gate cheap and quiet at the source

**Problem.** `scripts/check_health.sh` launches seven concurrent `uv run` processes on a possibly-2-vCPU machine, prints every passing log unconditionally, has no changed-files mode, and fails late; `code_health_gate.py --update-baseline` re-scans the whole repository; every new helper module in `core`/`data`/`accounting` must be hand-registered in `_LAYER_SPECS` in `packages/core/tests/test_architecture_dag.py`.

**Work.**
1. **Quiet by default, verbose on demand.** Print a one-line summary per check on pass; cat the log only on failure or with `--verbose`. On failure print the failing checks and a bounded tail.
2. **CPU-bounded parallelism.** Cap concurrent jobs at `nproc` and schedule the CPU-heavy checks (`basedpyright`, `complexipy`) deterministically on small machines, so wall time does not degrade under contention.
3. **`--changed [--base <rev>]` inner mode**, running only on touched files: `ruff check`/`format --check`, the LOC/token and broad-exception part of `code_health_gate.py`, `complexipy` on touched files, `basedpyright` on touched files, `check_js_modules.py` only when `static/**` changed, and `architecture_gate.py --fast` only when imports changed or files were added/moved. It must print which full checks it skipped.
4. **Zero-touch layer classification.** Classify `core.*`, `data.*`, `accounting.*`, `strategies.*`, `proof_runtime.*`, `web.*` from each package directory on disk (keeping `cli.py` special-cased and the frozen root shims explicit). Splitting a module must never require editing an architecture test.
5. **Exit status is authoritative.** No pipe without `pipefail`; a check that could not run reports `unavailable`, never `passed`. Add timeouts and process-tree cleanup.

**Acceptance.** `check_health.sh --changed` on a 2-file Python edit finishes in <2 s on a 2-vCPU runner and prints at most 3 lines on success; a new module under any `packages/*/src` passes the architecture tests with zero edits to test files; the full gate is materially quieter (target: no passing-log body unless `--verbose`).

**Risk.** Medium-low, and it is concentrated in one place: `_LAYER_SPECS` is currently the mechanism that prevents accidental layer violations. Filesystem classification must be proved equivalent to the current table on the current tree (a test that asserts the derived mapping equals the frozen `HEAD` mapping for all existing modules), otherwise the architecture gate silently weakens.

### S2 — Test routing with off-the-shelf selection, plus a scoped CI

**Problem.** Agents have exactly two options: guess one test file, or run everything. There is no `docs`-only path, and full runs occur after Markdown edits. CI runs every package leg for every push. The original S2 proposed hand-writing an impact planner from the package DAG; a spike (below) shows that file-level static selection would select most of the suite anyway, so that planner is dropped.

**Decision (2026-11-23).**

| Option | Verdict | Reason |
| --- | --- | --- |
| `pytest-testmon` 2.x (coverage-based, method-level, works with `-n`/`--dist loadfile`) | **Adopt, advisory only, inner loop** | Only option that shrank the selection materially in the probe (6 of 64 tests after one function-body edit). Reads uncommitted edits, needs no git base. Documented blind spots: static files, external services, and whatever coverage.py cannot see. |
| Path router in the repo's own style (stdlib script generalizing `scripts/should_run_c_tier2.py`) | **Adopt** | Mechanism already exists, is tested (`test_ci_code_health.py`), needs no `uv sync`, and adds no third-party action to a workflow with repo access. Handles docs-only, package scoping and the non-`.py` fallback that testmon cannot. |
| Tach / `grimp` import-graph selection | Not adopted for test selection | Works on the namespace layout (`tach map`, 0.6 s) but is too coarse here (spike). Both are static and miss dynamic discovery. Possible later aid for the S1 layer check, out of scope here. |
| Hand-written DAG impact planner (original S2.2) | Dropped | Would reproduce the coarse static result with extra code to maintain. |
| `dorny/paths-filter`, `tj-actions/changed-files` | Not adopted | Repo script already covers it; avoids a third-party action dependency. |
| `testmon.net` (`--tmnet`) | Not adopted | Ships coverage metadata of a private repository to a third party. |
| Pants / Bazel / Nx | Not adopted | Whole-build-system migration; violates "wrap existing commands". |

**Spike evidence** (2026-11-23, one sandbox, n=1 per probe, diagnostic only, not acceptance numbers). Environment: 2 vCPU, Python 3.13 (project pins 3.12), no `uv`, `pytest-testmon` 2.3.1 (published four days earlier), `tach` 0.35.3, `coverage` 7.17.4. One accounting test failed in every run because the probe environment had no installed CLI entry points; it is environmental, not a repo defect, and testmon correctly re-ran it every time.

| Probe | Result |
| --- | --- |
| Static reverse-import closure to **test files** (90 files, 16,886 LOC) | `core/contracts.py` -> 62 files / 74% LOC; `core/hashed_json_cache.py` -> 77 files; `proof_kernel.py` -> 40 files / 52%; `proof_runtime/cohort_models.py` -> 37 files / 51%; `web/server.py` -> 29 files / 48%; `accounting/ledger.py` -> 4 files / 4%. Only leaf modules benefit. |
| Dynamic-discovery blind spot | 10 of 90 test files (architecture DAG and drop-in, plugin integration, package compatibility, JS modules, browser runner, CI/health gate tests) are reachable from no non-test file. A static selector would never run them. |
| `tach` pytest plugin | Printed no impact summary in the probe; not verified, not relied upon. |
| testmon build with `-n 2 --dist loadfile` | Database built in one run (64 tests, 5.4 s on the sampled subset); no-change rerun executed only the previously failing test. |
| testmon after a one-line edit inside `ledger._round` | 6 of 64 tests re-ran (accounting and one strategies test file), 1.2 s. |
| testmon after appending a newline to `fixtures/research/offline.json` | No re-run of the passing tests. Inconclusive for this repo (not shown that those tests read it), but consistent with testmon's documented rule that static files are untracked. Therefore non-`.py` inputs go to the full fallback by rule. |
| Replay of the last 80 non-merge commits through a path classifier | 14 docs-only (17%); 37 (46%) touch at least one path outside `packages/*/{src,tests}` (root config, `scripts/`, `.github/`, `config/`, `.basedpyright/`, `pyproject.toml`, `uv.lock`); 29 (36%) are package-only: 6 need one package leg, 10 need two, 2 three, 1 four, 10 all six. Mean package legs per commit 3.98 of 6 (-34%). |

Reading of the evidence: method-level selection pays off in the inner loop; at CI level, scoping saves runner minutes and queue time, but wall time is bounded by the slowest leg (`proof` tests and `perf-equivalence`), so it shortens wall time only for docs-only and web-only pushes. Shortening the slowest leg is S4a's job, not S2's.

**Work.**
1. **One documented launcher**, `scripts/verify.py --profile quick|affected|full` (stdlib only, does not replace the health ratchet). It composes the S0/S1 pieces already on `main` instead of adding parallel ones: `scripts/changed_files.py` for the change set (`--base`, untracked files included), `check_health.sh --changed` for the lint/type/ratchet part, and `scripts/verify_record.py` for the measurement record of every step. Reports `passed` / `failed` / `not run` / `unavailable` per check group and one bounded summary. `full` is the unchanged health gate plus the full suite.
2. **Path router**, `scripts/change_scope.py`, shared by the launcher and CI. It takes its path list from `changed_files.changed_paths` locally and from the PR/push diff in CI (`should_run_c_tier2.py` keeps its CLI and delegates). Rules, evaluated per changed path; any unmatched path means full:
   - `docs/**` and root-level `*.md` only: docs profile (link/whitespace/secret checks), no `ruff`, `basedpyright` or `pytest`. Markdown **inside** `packages/**` is not docs: `packages/web/tests/test_http_routes.py` reads a vendored `README.md`, so such files take the non-`.py` rule below. A grep found no test that reads `docs/` or root Markdown content (`test_ci_code_health.py` only passes the string `docs/development/ci.md` to a pure function); keep docs-only as an explicit S3 shadow case.
   - `packages/<p>/{src,tests}/**/*.py`: package `p` plus its reverse dependents from the six-entry table `core -> all`, `data -> {data, strategies, proof, web}`, `accounting -> {accounting, strategies, proof, web}`, `strategies -> {strategies, proof, web}`, `proof -> {proof, web}`, `web -> {web}`. A test asserts this table against the architecture gate's layer classification so it cannot drift.
   - Non-`.py` files inside a package (static JS/CSS/HTML, JSON snapshots, manifests) and new or deleted files: full legs for the affected packages, no testmon deselection; `check_js_modules.py` when `static/**` changed.
   - Everything else (`pyproject.toml`, `uv.lock`, `.github/`, `config/`, `fixtures/`, root `tests/`, `scripts/`, `.basedpyright/`, `.jscpd.json`, root `src/`): full. This group is 46% of recent commits, so it decides most of the achievable saving; relax it per sub-path only on S3 shadow evidence, never before.
3. **testmon in the `affected` profile only**, as a dev dependency (root governance edit, coordinate per `AGENTS.md`; pinned via `uv.lock`). Database per worktree via `TESTMON_DATAFILE`, `.testmondata*` gitignored (this is not the forbidden central duration file). The profile always runs, regardless of selection: `architecture_gate.py`, `test_perf_equivalence.py` and `test_fastcore_parity.py` when `core`/`proof`/`accounting` changed, and every test that spawns subprocesses or the CLI or imports `proof_pool` (testmon coverage of child processes is unverified), for any package it touches. Browser smokes stay outside testmon (S4b). Selection is reported as "affected (advisory)", never "verified".
4. **Invocation hygiene** inside the launcher: `-n auto --dist loadfile -q --tb=short`, worker count capped to `nproc`, `MOEX_OFFLINE_ONLY=1`, so a sequential 300 s run cannot happen by accident.
5. **Scoped CI** (after S2 measurement confirms the router on shadow data): a first `scope` job (checkout, system `python3`, no `uv sync`) emits `docs_only`, the package list for the existing `package-tests` matrix, and `run_tier2`; downstream jobs get `if:` conditions. `architecture-gate` and `code-health` always run. `perf-equivalence` is skipped only for docs-only and web-only changes. A final always-running `ci-ok` job fails when any needed job failed or was cancelled (skipped is fine), giving one stable required-check name. `schedule`, `workflow_dispatch`, a missing or null base SHA, and any router error resolve to full, and the weekly scheduled run stays full as a backstop for routing mistakes.
6. **Transparency.** The launcher and the `scope` job print which groups were selected or skipped and why. A passing affected run is labelled as such, never as full verification.

**Acceptance.**
- Docs-only change: the launcher finishes in <1 s with no Python check started; CI runs zero package legs and one green `ci-ok`.
- Leaf edit with a warm testmon database: only testmon-selected tests run plus the always-run set; probe reference is 6 of 64 tests and about 1 s. Targets for `packages/web` leaf <8 s and an internal `packages/proof` change <25 s are validated in the S0 harness, not assumed.
- A web-only push runs one package leg plus the two always-on gates; a `core` push runs all six.
- Router table-drift test, non-`.py` fallback test and "unknown path -> full" test pass; every run states its skipped checks.

**Risk.** Medium-high, still the only slice that can hide a regression. It ships advisory: testmon is never the gate, the full gate runs before any push (S3), the weekly full CI is the backstop, and promotion needs S3 shadow results with zero missed failures. Specific hazards: the probe ran before S1 landed, so re-measure the always-run set against the new quiet gate; first database build runs the whole suite under coverage and is slower (unmeasured; measure it in S0); testmon 2.3.1 is days old (pin it, judge on shadow data); subprocess and C-extension paths are invisible to coverage-based selection (hence the always-run set); a router table that drifts from the package DAG (hence the drift test).

### S3 — Cadence change, agreed and reversible

**Problem.** In the P7.2 session, twelve micro-commits each paid the full gate, a push and a workstation restart, and each ended the turn; the P7.1 session, with one turn and one publish, finished an order of magnitude faster per unit of work.

**Work.**
1. **Separate atomic commits from turn stops.** Amend `AGENTS.md` and the current plan's P4 wording (which says "one cohesive slice = one commit & push" and "if remaining slices exist — list them and stop") so that within one turn an agent continues across internal sub-slices until the work package is complete, a genuine product/accounting decision is needed, or the turn nears its call budget (target ~75%). Stopping after a dozen calls to ask for "продолжи" is a defect, not compliance.
2. **Two-tier verification and publication.** Inner slices: `--changed` gate plus the affected profile plus an atomic local commit. End of a coherent deliverable: full applicable gate on the exact integrated tree, one push to `origin/main`, one workstation sync. Keep commits small and reversible; do not bundle unrelated features to amortize testing.
3. **Shadow the routing.** Run S2's testmon selection and path router alongside the unchanged full gate on >=30 candidates spanning docs, leaf, cross-package and parity-sensitive changes, plus injected known regressions (registry discovery, proof parity, fixture change, a JS module break) in disposable worktrees. Record missed failures.
4. **Prompt template** for multi-step tasks, carried in the plan/AGENTS edit, matching §4 S3 rules (autonomy rule, inner-slice checks, one final gate/push/sync).

**Acceptance.** >=30 shadow candidates, zero missed regressions, fail-to-full switch exercised; median task-to-verified-`main` time on matched low-risk tasks improves by >=30% without a covered-check regression; human continuations per work package drop to 0–1.

**Risk.** Medium. Two failure modes to watch: an agent declaring a task done without the final integrated gate, and a "shadow" that is only run on easy candidates. Both are covered by the guardrails in §2.

### S4a — Reduce the cost of the full suite itself

**Problem.** `packages/proof/tests/test_perf_equivalence.py` (1,285 LOC, and the largest baseline file) runs inside every default discovery, rebuilding workspaces and re-running cohorts with real worker pools; `test_architecture_dag_dropin.py` copies all package sources twice and spawns three AST subprocesses; several proof test files rebuild the same synthetic workspace dozens of times.

**Work.**
1. **Split the equivalence suite by trial tier, not by coverage**: a fast canary in default runs, full-resolution 1d / 1h / pool-and-C parity files in `--pre-push` and in the existing CI `perf-equivalence` job. Splitting also removes the `--dist loadfile` tail where one worker holds the file after the rest finish.
2. **Session-scoped read-only fixture templates** cloned per test (hardlink/copy-on-write where safe, copy where inputs are mutable), replacing repeated JSON generation.
3. **De-duplicate fixture preparation** across `packages/proof/tests`, keeping full-resolution cases for pair/calendar, daily/hourly, worker/fallback, deterministic hashes and ledger reconciliation. Compare test IDs and prior regression coverage before replacing any fixture; do not trim trials or histories in a parity test to hit a budget.
4. **Make `test_architecture_dag_dropin.py` in-process** (parameterized repo root or a lightweight synthetic overlay) instead of copying the workspace three times.
5. **Worker-count A/B on pinned commits/fixtures** (sequential, `-n 2`, `-n 4`) with cgroup-aware caps, judged on end-to-end time including queue and setup. No central duration file; no duration-based deselection.

**Acceptance.** >=25% lower p90 of the full offline suite on a pinned 2-vCPU runner; identical test-ID coverage and identical canonical outputs, repeated in a clean process with network disabled; `packages/core` tests no longer carry a 40 s-plus drop-in tail.

**Risk.** Medium-high. This is the slice where a "speedup" can silently delete a parity guarantee. Mitigation: any removed or merged test must be justified by an equivalent assertion that still runs somewhere in the mandatory path.

### S4b — Reduce browser smoke cost

**Problem.** The runner spawns one Python process, one server and one cold Chromium per scenario, rebuilds identical fixtures per script, and drives full-trial Optuna searches to assert DOM state.

**Work.**
1. **Phase timers first**: app startup, browser launch, fixture hydration, API compute, UI assertions — so the optimization targets the measured term.
2. **Shared browser, isolated contexts**: launch Chromium once per worker, one `BrowserContext` and one server per scenario, standardizing each suite on a callable entry point while keeping standalone execution.
3. **Fixture templates** cached by builder identity and cloned per suite.
4. **Bounded trial budget for UI-only assertions**, honored only under `MOEX_OFFLINE_ONLY=1` and only where the scenario asserts no numerical result; exact-trial scenarios opt out explicitly.
5. **Then** move assertions that carry no UI-specific behaviour to HTTP/JS contract tests, keeping the full UI runner as reference until the replacement passes clean and repeated runs without cache leakage.

**Acceptance.** All current browser scenarios in under 120 s on a pinned 2-vCPU runner (stretch: <45 s), 12/12 passing, no increased flakes, and no scenario loses its UI-specific assertion.

**Risk.** Medium. Shared-browser caching is the classic source of order-dependent flakes; acceptance requires two consecutive clean runs and no shared writable state between scenarios.

### S4c — Workstation handoff and real-data startup

**Problem.** `remote sync` always runs `uv sync --frozen`, unconditionally restarts `launchd` (dropping warm catalog and worker pools), and returns before loopback readiness, forcing a second command and producing HTTP-race failures. `remote bench` copies `data/` and `data-lake/` byte by byte and cold-syncs a candidate environment per run. Cold `/api/proof/catalog` on real workstation data still costs several seconds.

**Work.**
1. **Conditional sync**: no-op when HEAD is unchanged; `uv sync` only when the lockfile or a `pyproject.toml` changed (or `.venv` is missing); restart only when runtime paths changed — docs, tests, workflows and `AGENTS.md` fast-forward without touching the service; when a restart is needed, poll loopback readiness inside `sync()` and return the verified health payload in the same call.
2. **Copy-on-write candidate worktrees**: `cp -cR` on APFS with a read-only/symlink fallback elsewhere; reuse the root `.venv` with `PYTHONPATH` over the candidate sources when the patch does not touch dependency or native-build inputs; assert the loaded module actually comes from the candidate.
3. **Fingerprint-keyed derived catalog cache** on the workstation, keyed over schema version and `(name, mtime_ns, size)` of every contributing data file, so any in-place edit invalidates it immediately. Byte-for-byte equivalence with the uncached path must be covered by a test (including same-size same-mtime-tick edits).
4. **Explicit outcomes** for the handoff (`published`, `synced`, `health-confirmed`), bounded readiness retries instead of fixed sleeps, idempotence for a repeated SHA, and no market-data movement off the workstation.

**Acceptance.** Docs-only sync <1.5 s with no restart; runtime sync returns verified health and matching head in one command; cold catalog after restart <0.8 s with an identical payload hash; bench setup <4 s on APFS.

**Risk.** Medium. The catalog cache is the only place where a stale cache could change numbers; it is gated on a determinism test rather than on inspection.

### S5 — Typing barrier and inspection tooling

**Problem.** Enabling strict typing across packages hits thousands of diagnostics, including a large class of namespace-package false positives; untyped JSON boundaries in `core` caused repeated edit-check cycles. Separately, ~47% of tool text is shell-read file dumps, and edits go through fragile in-place string surgery.

**Work.**
1. **Fix namespace resolution deterministically** for the multi-root `moex_spread_lab` layout (a generated, gitignored unified view plus explicit type-check paths), so cross-package types resolve and genuine missing imports are visible again.
2. **Typed JSON boundary helpers** in `core` (`load_object`, `as_str_dict`, a canonical `JsonValue`) to remove the dominant cause of strict-mode noise at `json.load` boundaries.
3. **Incremental strict onboarding**, one leaf-module group at a time (start with modules already extracted cleanly), never a bulk flip; each step lands with a green gate.
4. **A compact inspection CLI**: `outline <file>` (imports, signatures, line ranges, LOC/tokens, per-function complexity), `callers <symbol>` (production vs test vs compatibility-facade callers), and `check <files>` (single-file ruff + format + LOC/token + complexity + types, sub-2 s) so ratchet ceilings are discovered before writing a module, not after.
5. **Codemods for repetitive rewrites** (import-only or re-export moves) with idempotence and diff review.

**Acceptance.** Zero false `reportMissingImports` on the namespace layout; strict mode expands module group by module group with a green gate each time; `inspect check` on a 2-file edit <2 s and catches a deliberately introduced ceiling breach; `Bash` `sed`/`cat` file dumps per session materially reduced (target: >50%) with edits made through structured tools or codemods.

**Risk.** Low-medium. The main hazard is turning strictness off again to make a gate green; the acceptance rule is that a group either qualifies or is not onboarded yet.

### S6 — Warm verification lane (conditional)

Enter only if, after S1–S4, setup/queue time or repeated cold runs are still a material share of the end-to-end metric.

**Work.** Compare a prebuilt versioned runner image with ephemeral per-job worktrees against a managed warm pool, keyed on lockfile/OS/arch/toolchain/flags, using the exact same integrated tree; publish only the checked tree and invalidate when `main` advances. A merge queue is an owner decision, not part of this plan's rollout. Extend the existing `remote` CLI rather than replaying shell.

**Acceptance.** >=25% lower p90 ready-to-verified-`main` than the improved cold lane, without worse flakiness or unacceptable cost. Otherwise stop and record why.

**Risk.** Medium-high on cost and on hidden state leakage. Default expectation: not needed.

---

## 5. Verification-target summary

| Slice | Acceptance target (proposed, not measured) | Mandatory guardrail |
| --- | --- | --- |
| S0 | Three replay baselines published with sample counts | No number used as evidence without a baseline row |
| S1 | `--changed` <2 s, <=3 lines on pass; new module needs zero test edits | Derived layer mapping proven equal to the frozen table |
| S2 | docs <1 s; leaf edit selects only testmon-affected tests; CI runs 0 package legs for docs-only; skipped checks printed | Non-`.py` / unknown / shared change -> full; testmon never authoritative |
| S3 | >=30 shadow candidates, 0 missed regressions, -30% median task-to-verified time | Final integrated gate before push; fail-to-full switch |
| S4a | -25% p90 full offline suite on pinned 2-vCPU | Identical test IDs and canonical outputs; no trimmed parity trials |
| S4b | All browser scenarios <120 s on pinned 2-vCPU | Two consecutive clean runs, no shared mutable state |
| S4c | Docs sync <1.5 s; single-command verified sync; catalog <0.8 s | Byte-identical catalog payload; cache invalidation test |
| S5 | 0 false missing imports; incremental strict onboarding | Onboarding is per group, gate green at every step |
| S6 | -25% p90 vs improved cold lane | Same tree, invalidate on `main` advance |

Two structural checks that no slice may weaken: proof/accounting numerical equivalence and replay coverage stay mandatory whenever those paths change, and the browser, macOS and C99 sanitizer gates stay mandatory when their contracts are touched.

---

## 6. First work packet

**Do S0 and S1 only.** They are additive, carry no numerical risk, and their measurement is what decides whether S3's cadence change and S4's cost work are justified. Concretely: instrument the existing loop, publish the three baselines, then land the quiet/CPU-capped/`--changed` full gate with filesystem layer classification and an equivalence test for that classification. Choose between S4a and S4c as the next major investment from the S0 baseline, not from the estimates in this document.

Out of scope for the first packet, and for this plan: `F2` pipeline unification, CI workflow restructuring, and any automatic promotion of a verified `main` to the workstation.

---

## 7. Security and hygiene follow-ups

- The three analysed exports contain credentials and are not tracked. The owner must rotate the exposed GitHub PAT and bridge token, and check any remaining copies or artifacts derived from the exports.
- Never embed credentials in remote URLs or `.git/config`; use an untracked, `0600`, gitignored runtime env file, and read it only after verifying it is untracked.
- No telemetry record, prompt template or committed artifact may contain tokens, account data, absolute home paths or raw exports.
- `.typecheck_typings`, catalog caches, candidate worktrees and measurement records are generated and gitignored; they must never be committed.

---

## 8. Evidence base

| Export | Task | Bash calls | Summed call durations | Reporting caveat |
| --- | --- | ---: | ---: | --- |
| `fb9e1ee.md` | P1, P2, P3, rounding fix | 397 | 7,820 s | 22 full suite runs; 151 sub-second calls; ~24 pushes and workstation syncs |
| `b687ae2.md` | P7.1 strategy discovery and lazy registry | 127 | 349 s | 1 assistant turn, 0 human interruptions; 100 sub-second calls; this is the control case |
| `9dae286.md` | P7.2 proof decomposition (partial) | 161 | 1,821 s | 12 full suites (79–89 s each) and 10 "продолжи" waits for a partial multi-slice refactor |

Read these as three different workloads on different revisions and environments. The relative shares are usable for prioritization; the absolute totals are not a benchmark.
