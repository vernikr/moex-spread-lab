# Remote agent testing and benchmarking via workstation bridge

Status: **Partially implemented**: the macOS launch agent, remote status/sync/probe/bench, discovery file and initial loopback diagnostics are implemented; progress-stage and instrumented operation wall timings, request-thread and worker-task cache counters, and a best-effort pool census are available.

## 1. Current capabilities vs actual friction

### 1.1 Provided facts (Arena Bridge)

Cloud agents do **not** have to read the application through a browser. The workstation bridge already exposes direct non-browser primitives over HTTPS (`Authorization: Bearer $ARENA_BRIDGE_TOKEN`):

- **Filesystem (read-only, approval-free):** `list_dir`, `read_file`, `search_files`, `GET /files?path=...`, `file_get`.
- **Command and process execution (side-effecting, subject to trust window / local approval):** `POST /exec`, `POST /call` (`bash`), `start_process` / `read_process_output` / `kill_process`.
- **Loopback HTTP:** `screener_app.py` binds `127.0.0.1` and already serves a typed JSON/NDJSON API (`/api/settings`, `/api/timeframes`, `/api/proof/catalog`, `/api/proof/run`, `/api/proof/optimize`, `/api/proof/plateau`, `/api/proof/windows`) without browser rendering. A local `curl` executed on the workstation hits those endpoints directly.

### 1.2 Observed operational gaps (dated workstation inspection, 2026-11-21)

Although browserless transport exists, ad-hoc testing by cloud agents exhibits six structural problems:

1. **No canonical daemon or service discovery.** Agents spawn ad-hoc servers on arbitrary ports or bypass the HTTP server with inline Python scripts in `/tmp`. Orphaned `screener` processes and `spawn` worker pools accumulate and contend for CPU/RAM (`loadavg` > 3.5 during measurements), distorting latency numbers.
2. **Cold vs warm state conflation.** Direct `python -c` invocations construct a fresh process, cold file caches and uninitialized `CohortWorkerPool`, whereas the real user server keeps `_HashedJSONCache`, `CalendarData` and sticky worker shards warm across requests.
3. **Mutated user state and race conditions.** Testing against a live checkout mutates `data/analytics/research-settings.json` and appends trial logs to `data/analytics/*.jsonl`, changing user preferences and invalidating open browser tabs.
4. **Checkout Contamination & Git Drift.** Testing unpushed edits directly inside the primary checkout (`~/Downloads/projects/moex-spread-lab`) violates the fast-forward-only workstation handoff rule and risks breaking local work.
5. **Giant UI payloads over remote bridge.** `/api/proof/run` and `/api/proof/optimize` return full chart series, daily ledgers and trade cards (hundreds of kilobytes to megabytes), wasting bridge bandwidth and LLM context tokens when the agent only needs wall/CPU timings, `run_id`, `rank_score` and reconciliation checks.
6. **Approval friction on `bash`.** Generic `bash` calls are side-effecting in Arena Bridge: outside an active trust window, every `curl` or benchmark invocation blocks on manual approval.

---

## 2. Design goals and non-goals

### Goals

- **Zero browser overhead** for functional, equivalence and latency testing (`chrome_*` reserved strictly for DOM/Canvas UI smoke tests).
- **Zero new runtime dependencies:** Python 3.12 stdlib (`urllib.request`, `http.server`, `argparse`, `time`, `tracemalloc`, `cProfile`) + existing locked packages (`duckdb`, `pyarrow`, `optuna`, `zstandard`).
- **Hermetic state isolation:** agent runs never overwrite user `research-settings.json` or `data/analytics/*.jsonl` and never dirty the primary git checkout while shares of read-only market data (`data/`, `data-lake/`) remain zero-copy.
- **Deterministic server-side telemetry:** separate cold-start vs warm-cache vs hot-repeat timings, per-phase breakdown, worker pool metrics and bit-for-bit `run_id` / `rank_score` parity assertions.
- **Keep-loopback security:** `moex-spread-lab` stays bound strictly to `127.0.0.1` with no public port exposure; remote access flows exclusively through the authenticated workstation bridge.

### Non-goals

- No cloud upload or redistribution of MOEX/local market datasets.
- No broker connectivity, live trading, order routing or VPS deployment.
- No weakening of `Host` / `Origin` loopback protections in `screener_app.py`.

---

## 3. Architecture and stack

```text
+-------------------------------------------------------------------+
| Cloud Agent Sandbox (/home/user/webapp)                           |
|                                                                   |
|   uv run moex-spread-lab remote <subcommand>                      |
|     reads: ARENA_BRIDGE_URL, ARENA_BRIDGE_TOKEN (env only)        |
+---------------------------------+---------------------------------+
                                  | HTTPS (Bearer header, compact JSON)
                                  v
+-------------------------------------------------------------------+
| Workstation Bridge (Arena Bridge on macOS)                        |
|   POST /exec  |  GET /files (read-only telemetry artifacts)       |
+---------------------------------+---------------------------------+
                                  | local loopback / CLI
                                  v
+-------------------------------------------------------------------+
| Workstation Checkout & Ephemeral Worktrees                        |
|                                                                   |
| 1. Canonical Baseline Daemon (launchd user agent, 127.0.0.1:8765) |
|    - Tracks clean `main` checkout                                 |
|    - Serves UI + read-only `/api/dev/*` introspection & bench     |
|    - Writes `data/analytics/server-instance.json` (port, pid, sha)|
|                                                                   |
| 2. Ephemeral Candidate Sandbox (`git worktree` + symlinked data)  |
|    - Isolated code revision or unpushed patch                     |
|    - Isolated temp `settings` / `analytics` dir                   |
|    - Zero-copy read-only view of `data/` and `data-lake/`         |
|    - Runs `moex-spread-lab bench` or ephemeral loopback port      |
+-------------------------------------------------------------------+
```

### Stack choices

| Layer | Choice | Rationale |
|---|---|---|
| Transport to workstation | Arena Bridge HTTP API (`POST /exec`, `GET /files`) wrapped in `moex_spread_lab.remote_cli` | Eliminates manual `curl` quoting, enforces timeouts, parses structured JSON and strips credentials from logs. |
| Local server runtime | Existing `ThreadingHTTPServer` (`127.0.0.1`) managed by macOS `launchd` (`KeepAlive`, `ProcessType=Interactive`) | Single canonical instance on `8765`, clean child-pool lifecycle, no orphaned background processes. |
| Candidate isolation | `git worktree` under `~/.cache/moex-spread-lab/worktrees/<id>` + directory symlinks to `data/` and `data-lake/` | Instant zero-copy access to all local market data and sealed H1 Parquet tables without touching the primary working tree or copying hundreds of megabytes. |
| Request isolation | Per-request `settings_override` (in-memory `ResearchSettings`) + `ephemeral=True` (no JSONL history writes) | Allows testing arbitrary timeframes, capital, slippage, seeds and V/C screens concurrently without mutating `research-settings.json`. |
| Profiling & timings | `time.perf_counter_ns()`, `time.process_time_ns()`, `resource.getrusage`, optional `cProfile` top-N summary | Sub-millisecond wall/CPU split and memory RSS high-water mark with near-zero overhead. |

---

## 4. Technical design of components

### 4.1 Server discovery & introspection endpoints (`screener_app.py`)

Keep `Host` and `Origin` checks restricted to `127.0.0.1:{port}` / `localhost:{port}`. Add three lightweight loopback endpoints:

1. **`GET /api/dev/health`** — fast readiness and provenance check (<5 ms):
   - `pid`, `port`, `started_at`, `uptime_sec`
   - `git_head`, `git_dirty`
   - `python_version`, `fastcore_compiled` (`bool`), `fastcore_path`
   - `configured_workers`, `active_pool_workers`, `loadavg_1m`
   - `data_fingerprint` (existence/seal status of `current-intake.json`, `lqdt-lineage.json`, `h1-seal.enabled.json`, `calendar` cache count — no workstation usernames or secrets)
   - Active lock states (`search_busy`, `profile_busy`, `windows_busy`).
2. **`POST /api/dev/probe`** — stateless API wrapper over existing `ProofService`:
   - Accepts `{ "action": "run" | "optimize" | "plateau" | "windows" | "catalog", "payload": {...}, "settings_override": {...}, "response_mode": "summary" | "full", "repeat": 1, "warmup": 0, "profile": false }`.
   - Applies `settings_override` purely in memory for that call (`ResearchSettings` passed explicitly; `research-settings.json` untouched).
   - Never writes to `screener-runs.jsonl` or `proof-searches.jsonl`.
   - In `response_mode="summary"` (default), strips `series`, `ledger` and full `trades` arrays, returning compact deterministic verification fields:
     - `run_id`, `selection`, `rank_score`, `evaluation` summary, `trade_count`, `ledger_reconciliation_rub`, `account_checks`, `plateau` scores/colors, `seed_used`, `trial_count`, `pruned_count`.
     - `telemetry`: `wall_ms` (per-iteration list + min/median/p95), `cpu_user_ms`, `cpu_sys_ms`, `max_rss_kb`, phase breakdown (`prepare_cohort_ms`, `optuna_trials_ms`, `plateau_ms`, `final_report_ms`), cache counters (`json_cache_hits`, `z_cache_hits`, `pool_used`, `shard_count`), and optional top-20 `cProfile` cumulative functions when `profile=true`.
3. **Runtime discovery file (`data/analytics/server-instance.json`, gitignored):**
   - Written atomically on `serve()` startup and removed on clean shutdown; records `port`, `pid`, `started_at` and `git_head` so local CLI commands locate the running daemon automatically and detect stale PIDs.

### 4.2 Deterministic benchmark suites & local CLI (`moex-spread-lab bench`)

Add a first-class subcommand `uv run python -m moex_spread_lab.cli bench` with curated, versioned benchmark suites (`config/benchmarks/*.json`) covering realistic workstation workloads:

| Suite ID | Workload covered | Primary metric |
|---|---|---|
| `catalog_boot` | `ProofService.catalog()` on `1d` and sealed `1h` | Cold vs warm catalog latency |
| `single_proof_1d_1h` | `GLDRUBF/GLZ6` and `CNYRUBF/CRZ6` `max_affordable` replay (`1d` + `1h`) | Replay + C99 `size_proof_metrics` wall time & `run_id` |
| `calendar_proof` | Native calendar `SRU6/SRZ6` (`zscore_mean_reversion` + `convergence_to_ruonia`) | Calendar series + reconciliation parity |
| `window_table_batch` | `/api/proof/windows` across all historical `CNYRUBF` / `USDRUBF` maturities | Sharded `StudyWindow.report` throughput |
| `cohort_optimize` | `/api/proof/optimize` (`optimize_strategy=true`, fixed `random_seed=41`, 10+ checked windows) | Full 80-trial/family Optuna + `Canary + Fan-Out` + plateau latency & exact `rank_score` |
| `ab_fastcore` | Same `cohort_optimize` comparing default C99 vs `MOEX_PROOF_DISABLE_C=1` / `MOEX_PROOF_WORKERS=1` | Speedup factor + bit-for-bit `run_id` / `rank_score` equivalence |

Execution modes of `moex-spread-lab bench`:
- `--mode http` (default when daemon is up): sends `/api/dev/probe` requests to the live `127.0.0.1` server, measuring real HTTP + worker-pool behavior.
- `--mode inproc`: instantiates `ScreenerStore` in-process, explicitly measuring **cold** (first call in fresh interpreter) followed by **warm** (subsequent calls with populated caches and warm worker pool).
- `--compare-ref <git-ref>` (workstation helper): spins up an ephemeral read-only `git worktree` for `<git-ref>`, runs the identical suite on baseline and candidate back-to-back under quiescent CPU check, and emits a structured delta table (`speedup_x`, `wall_ms_delta_pct`, `exact_match: bool`) into `data/analytics/bench-reports/<run_id>.json`.

### 4.3 Cloud-side thin client (`moex-spread-lab remote`)

Add `src/moex_spread_lab/remote_cli.py` wired as `moex-spread-lab remote ...` for cloud agents. It reads `ARENA_BRIDGE_URL` and `ARENA_BRIDGE_TOKEN` from environment variables (never CLI flags or committed config):

- `moex-spread-lab remote status`
  Checks Arena Bridge `/health`, inspects workstation CPU load (`loadavg`), checks git `HEAD` of the workstation checkout, and queries `GET /api/dev/health` on the local daemon (reporting if any stray Python benchmarks are currently saturating CPU before timing runs begin).
- `moex-spread-lab remote sync`
  Fast-forwards `~/Downloads/projects/moex-spread-lab` from `origin/main` (`git fetch origin && git merge --ff-only origin/main`), runs `uv sync --frozen` if `uv.lock` changed, recompiles `_fastcore` if `_fastcore.c` changed, and gracefully restarts the `launchd` server so warm caches match `HEAD`. Refuses to touch dirty tracked files.
- `moex-spread-lab remote probe --action optimize --json '{...}' [--settings '{...}'] [--profile]`
  Executes a single compact API probe against the workstation server and returns the ~1–2 KB summary JSON to stdout.
- `moex-spread-lab remote bench --suite cohort_optimize [--repeat 3] [--worktree-patch HEAD]`
  Runs a named benchmark suite on the workstation. With `--worktree-patch`, uploads the current cloud diff into an ephemeral workstation `git worktree` (linked to the real `data/` and `data-lake/`), runs A/B timing + bit-for-bit equivalence against `main` **before** pushing to `main`, and cleans up the worktree in `finally`.

#### Implemented status/sync commands

Set `ARENA_BRIDGE_URL` (HTTPS) and `ARENA_BRIDGE_TOKEN` in the calling environment; optionally set `MOEX_WORKSTATION_ROOT` to the remote checkout path. `uv run moex-spread-lab remote status` reports checkout HEAD/branch, tracked dirtiness, load average, launchd presence and loopback HTTP status; the loopback `/api/dev/health` endpoint supplies additional process metadata. `uv run moex-spread-lab remote sync` refuses tracked changes, non-main branches and missing launchd service, then fetches and fast-forwards `main`, runs `uv sync --frozen`, checks fastcore and rebuilds it when its C source is newer, and requests a launchd restart. Untracked files are preserved and Git refuses collisions. Re-running sync reconciles dependencies even if a prior attempt failed after the fast-forward. The response reports the restart request, not readiness: run `remote status` afterwards to verify HTTP 200. Failures after a fast-forward do not roll back the checkout. Tokens are never passed as arguments or in URLs; redirects are rejected. `remote probe --action catalog|run|optimize|plateau|windows --json '{...}' --settings '{...}'` invokes the loopback `/api/dev/probe` with validated, in-memory settings and no search-history persistence. `--repeat`, `--warmup`, `--profile`, and `--full` are optional; default summary is compact. Top-level CPU and peak RSS values cover the server process, not worker children; a profile may exceed the normal 2 KB summary budget; progress-stage wall intervals are approximate. `operation_phase_ms` measures explicitly instrumented request-thread calls to cohort preparation, Optuna tuning, report and plateau, including time waiting for worker results; nested calls of the same phase are not double-counted. Values are summed across repetitions, exclude warm-up, need not sum to total wall time, and are not worker CPU time. Phases not executed are absent. `cache_metrics_main_thread` counts JSON and zscore lookups in the request thread only (not worker threads or child processes). `worker_task_metrics` aggregates JSON/zscore cache lookups and user/system CPU deltas for successfully completed pool tasks submitted during measured repetitions (not warm-up or asynchronous cleanup). Its worker count is the number of distinct PIDs that returned measured tasks; concurrent non-probe work and task failures are not attributed. Workers execute serially within each pool slot, but process CPU includes any other threads in that worker during a task. Probe instrumentation adds IPC and timing overhead. Per-request worker peak RSS is not measured. `pool` is a best-effort snapshot of initialized executor slots and live registered child processes, using private executor internals. Probe-only `pool.worker_resources` samples the current RSS and cumulative lifetime CPU of live pool workers via `ps`; it is not per-request CPU, excludes exited workers and other children, and `complete=false` indicates an incomplete or unavailable sample. `remote bench --suite catalog_boot [--repeat N] [--warmup N] [--worktree-patch HEAD]` runs a deterministic probe suite; other supported suites are shown by `remote bench --help`, and `custom` accepts `--json` with `action`, `payload`, and `settings_override`. The default is one warm-up per workload on each server; `--warmup 0` may compare a warm live server with a cold candidate, so speedups can be misleading. A per-checkout lock rejects concurrent benchmarks, busy proof locks cause an immediate refusal and load average is reported. With `--worktree-patch HEAD`, the workstation HEAD must equal the cloud HEAD; tracked changes are transferred (maximum 16 KB), but untracked files are not. The candidate uses a detached temporary Git worktree, its own locked environment and copied market data, excluding analytics except settings; cleanup is attempted in `finally`. A copied H1 seal is first integrity-checked and re-bound to the temporary root; the original seal is never modified. Copying deliberately trades zero-copy speed for isolation; the trusted candidate is not an OS security sandbox. Baseline and candidate full JSON results are compared by SHA-256 after normalizing only absolute root-contained `provenance.*_path` values to `$ROOT/` relative paths; all other fields, including trades and ledgers, remain unchanged. Only hashes and compact metrics cross the bridge. A mismatch fails the command. The suites include fixed-size 10-window Si calendar workloads and `ab_fastcore`, which runs two isolated servers (C enabled with six workers, C disabled with one worker) and compares full result hashes. C and worker count both change, so speedup is not attributable to C alone. Historical perpetual-quarterly maturity coverage, standalone Phase 2 bench modes/reports, comprehensive sub-operation timings and per-request worker peak RSS are not yet implemented. `fastcore_compiled` reports whether a compiled binary exists, not whether it is loaded or current.

### 4.4 Reducing approval friction (Optional Arena Bridge enhancement)

With the CLI above, all remote actions inside an active `arena-bridge trust` window work immediately with zero changes to Arena Bridge. To make read-only benchmarking completely approval-free even when the trust window is closed, two small optional additions on the workstation side suffice:

1. **`launchd` user service (`~/Library/LaunchAgents/local.moex-spread-lab.screener.plist`):** keeps `moex-spread-lab screener --port 8765` always up on `127.0.0.1:8765`, eliminating `start_process` / `kill_process` calls.
2. **File-backed telemetry or narrow read-only bridge tool:**
   - Anytime `bench` writes `data/analytics/bench-reports/latest.json`, agents can read historical baselines via approval-free `read_file` / `GET /files`.
   - Optionally register one allowlisted read-only tool in Arena Bridge (`moex_dev_probe` → `POST http://127.0.0.1:8765/api/dev/probe` only), allowing cloud agents to run arbitrary strategy/performance probes with zero shell access and zero human approval prompts.

---

## 5. Security, safety and data-governance invariants

1. **Loopback-only server:** `screener_app.py` never binds `0.0.0.0` and never relaxes `Host` / `Origin` checks. `/api/dev/*` is reachable only from `127.0.0.1`.
2. **Zero secret persistence:** `ARENA_BRIDGE_URL`, `ARENA_BRIDGE_TOKEN` and workstation paths/usernames are supplied via environment variables (`MOEX_WORKSTATION_ROOT` defaulting to `~/Downloads/projects/moex-spread-lab` resolved on the remote shell), never committed to repository files, manifests or test fixtures, and checked by `moex-spread-lab secret-scan`.
3. **Read-only market data & sealed H1:** ephemeral benchmark worktrees use separate copies of `data/` and `data-lake/`; `remote` and `bench` commands never invoke `data-refresh`, `data-gc --apply` or `data-h1-archive --apply`.
4. **Quiescence guard:** `bench` records `loadavg` and warns/fails if concurrent benchmark processes or active `proof_search_lock` contention are detected, preventing misleading latency claims from CPU-starved runs.
5. **Equivalence gate:** any A/B performance run asserts exact equality of `run_id`, `rank_score`, `plateau` scores and `ledger_reconciliation_rub == 0.0` alongside wall-time speedup (`docs/development/performance-optimization.md` §1).

---

## 6. Phased implementation plan

Each slice is independently testable, backward-compatible and covered by unit tests on synthetic fixtures before workstation verification.

### Phase 1 — Stateless probe API & server introspection (core repo)
- Add `settings_override: ResearchSettings | None` plumbing and `ephemeral` mode to `screener_app.py` so API probes never touch `research-settings.json` or `*.jsonl`.
- Implement `GET /api/dev/health` and `POST /api/dev/probe` (`summary` vs `full`, `repeat`, `warmup`, phase timings, RSS/CPU stats, optional `cProfile` summary) plus `data/analytics/server-instance.json` lifecycle.
- **Verification:** unit tests in `tests/test_dev_probe.py` verifying loopback-only 403 guards, zero file mutations on `research-settings.json`, schema compactness and exact `run_id` match against `/api/proof/run` and `/api/proof/optimize`.

### Phase 2 — Deterministic `bench` CLI & canonical suites (core repo)
- Add `config/benchmarks/default-suites.json` and `src/moex_spread_lab/bench.py` (`moex-spread-lab bench --mode http|inproc`, cold/warm split, quiescence check, JSON/table output).
- Add A/B comparison helper (`--compare-ref` using temporary `git worktree` + data symlinks + automatic cleanup).
- **Verification:** offline fixture tests for `bench` CLI (`inproc` and loopback `http`), worktree cleanup on error, and non-equivalence exit code on injected drift.

### Phase 3 — Cloud-agent `remote` CLI client (core repo + docs)
- Implement `src/moex_spread_lab/remote_cli.py` (`status`, `sync`, `probe`, `bench`, `--worktree-patch`) over stdlib `urllib.request` against `ARENA_BRIDGE_URL` / `ARENA_BRIDGE_TOKEN`.
- Document exact agent workflow in `AGENTS.md` and `docs/development/remote-agent-testing.md` so cloud agents use `moex-spread-lab remote ...` instead of ad-hoc `/tmp` scripts, manual port selection or browser scraping.
- **Verification:** mock-HTTP unit tests for `remote_cli.py` (auth header handling, no token leakage on error, timeout/approval handling) + live end-to-end run against the workstation checkout.

### Phase 4 — Workstation daemonization & optional read-only bridge tool (workstation ops)

**Delivered slice:** `moex-spread-lab install-workstation-daemon --root <checkout> --dry-run` previews the user agent; replace `--dry-run` with `--apply` to install and bootstrap it on macOS. The installer refuses to overwrite an existing plist or use an occupied port. It runs the checkout's named `scripts/moex-spread-lab-screener` launcher (which execs `.venv/bin/python3`) on port 8765, logs under `~/Library/Logs/`, and does not sync dependencies. After an update, restart with `launchctl kickstart -k gui/$(id -u)/local.moex-spread-lab.screener`. Remove the agent with `launchctl bootout gui/$(id -u)/local.moex-spread-lab.screener` and delete its plist only if intentional. The CLI handles SIGTERM so server shutdown invokes store cleanup. Workstation installation and process cleanup must be verified separately; this is not the full P3 implementation.

- Add template `config/launchd/local.moex-spread-lab.screener.plist.example` (no hardcoded user paths) and a setup helper `moex-spread-lab install-workstation-daemon --dry-run|--apply` to keep `127.0.0.1:8765` running cleanly with `CohortWorkerPool` cleanup on restart.
- Optional: add `moex_dev_probe` read-only tool to Arena Bridge so cloud agents can benchmark without opening a shell trust window.
- **Verification:** verify single canonical process on port `8765`, zero orphaned multiprocessing workers across restarts, and <2 s end-to-end cloud-to-workstation probe round-trip.


### Workstation verification (2026-11-22)

On macOS, a three-window 1h calendar probe started three pool workers. A launchd
restart replaced the server and all four old child processes (three workers and
the multiprocessing helper) exited; the replacement server returned HTTP 200.
Five warm cloud CLI catalog probes took 1.782, 1.235, 1.134, 1.101 and 1.165 s
end to end (median 1.165 s); the first catalog request after server restart is
cold and can exceed 2 s. This is not a latency guarantee under load.

Earlier versions of the `single_proof_1d_1h`, `calendar_proof`, `window_table_batch`
and `cohort_optimize` suites completed once on the Mac. At that point the
window workload contained one window and optimization recorded 40 trials;
these were expanded afterwards as documented below. The 1d single-proof
result reported `ledger_reconciliation_rub = -0.01`; consequently the
specified zero-reconciliation acceptance criterion is **not met**. A full
workload/equivalence audit and missing standalone Phase 2 modes remain outstanding; the basic suite runs are not evidence of those features.


The expanded suites (2026-11-22) use both requested perpetual pairs at 1d/1h
with `max_affordable`, both calendar strategies, ten Si calendar windows, and a
ten-window 80-trial strategy search. The four max-affordable examples each
reported zero reconciliation; this does not erase the separate -0.01 RUB
fixed-size case. The fastcore A/B run (C plus six workers versus Python plus
one worker) produced matching full-result hashes and 2.006 s versus 7.594 s
once after warm-up; this changes both engine and worker count. Before path
normalization, the single-proof worktree comparison failed because provenance
contained different absolute checkout paths despite identical run IDs. Only
root-contained provenance paths are now normalized, not numeric or ledger data.

After root-bound provenance path normalization, the four max-affordable single-proof workloads also matched the isolated worktree results on the Mac, including full ledger data.
