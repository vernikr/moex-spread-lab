# Agent Session Latency & Context-Bloat Prevention Plan

Status: **Actionable engineering & workflow plan** (forensic post-mortem of the 2026-11-22 agent transcript + prioritized technical and operational mitigations).

---

## 1. Executive Summary & Forensic Post-Mortem

On 2026-11-22, a single cloud-agent session completing Step 4 Phases 4–5 of [`code-health-plan.md`](code-health-plan.md) ran for **over 4.5 hours of wall-clock time** (`11:49 AM -> ~4:25 PM`), spanned **12 messages (6 assistant turns)**, hit the **per-turn tool-call ceiling twice** (requiring manual `"продолжи"` interventions), executed **719 `Bash` calls**, accumulated **221.4 minutes (`13,286 s`) of active shell runtime**, and bloated the exported transcript to **29,777 lines / 1.73 MB (~480k tokens)**.

### Quantitative Breakdown of the Session

| Activity Category | `Bash` Calls | Active Shell Time | Share of Runtime | Transcript Volume (chars) | Root Cause |
|---|---:|---:|---:|---:|---|
| **1. Browser / Playwright UI smoke & ad-hoc `/tmp/diag*` probes** | 66 | **130.0 min** (`7,799 s`) | **58.7%** | 104,511 | `MOEX_OFFLINE_ONLY=1` missing in 10/11 browser smoke scripts -> `CalendarData._load()` hit live `iss.moex.com` for ~1,050 uncached contract chains (**~181.5 s per run**); plus missing static JS symbol check caused 4 browser round-trips (`~20 min` each) to catch 4 `ReferenceError`s (`$`, `state`, `activeStrategyName`, `td`). |
| **2. Code-health gates & linters (`ruff` / `basedpyright` / `complexipy`)** | 179 | **30.6 min** (`1,836 s`) | **13.8%** | 393,641 | Re-running full workspace gates (`18–23 s` each) on intermediate edits instead of single-file checks (`<0.5 s`) until slice completion. |
| **3. `pytest` execution (full suite & `packages/proof/tests`)** | 36 | **30.0 min** (`1,797 s`) | **13.5%** | 49,257 | Running full 511-test workspace suites (`~200 s` on 2 vCPUs) repeatedly during debugging and polling background runs with `sleep 10` loops (`401 s` wasted on Turn 2). |
| **4. File writing & editing via `Bash` (`cat <<EOF`, inline `python3 - <<PY`)** | 99 | **10.2 min** (`614 s`) | **4.6%** | 195,729 | Agent lacked or bypassed structured Read/Edit tools, echoing entire source files into `Bash` heredocs and writing ad-hoc regex splitters that silently dropped imports. |
| **5. Workstation bridge discovery & temporary CI handoff** | 36 | **9.0 min** (`542 s`) | **4.1%** | 39,356 | Org secrets (`ARENA_BRIDGE_*`) are unreadable via GitHub REST API; agent committed 2 temporary workflows to `main` (`f1f4d7b`, `96a15e2`) + revert (`036a8c8`) to extract bridge credentials via Actions artifacts. |
| **6. Blind `sleep` polling loops (`sleep 90..285`)** | 4 (+ embedded) | **8.5 min+** (`510 s+`) | **3.8%+** | 2,339 | Using `nohup ... & sleep 285` with fixed upper-bound sleeps instead of event-driven process exit (`timeout 300 cmd`), wasting full sleep budgets even when jobs crashed in `2 s`. |
| **7. File reading / searching via `Bash` (`sed -n`, `grep`, `cat`)** | 278 | **2.9 min** (`174 s`) | **1.3%** | **555,306** | **#1 source of context bloat (40.6% of all tool characters):** 278 shell reads dumped raw code slices into prompt history, slowing LLM prefill/thinking on every subsequent call. |
| **Other inspection / git / env setup** | 21 | **0.2 min** (`13 s`) | **0.1%** | 26,759 | Normal overhead. |
| **Total (`Bash` + 218 Thought blocks @ 9.9 min)** | **719** | **221.4 min** | **100%** | **1,617,964** | **2 hard tool-limit stops (Turns 4 & 8)** |

### Turn-by-Turn Escalation Profile

- **Turn 2 (`11:49 AM`, 28 calls, 8.1 min):** Cloned repo, ran `uv sync` + `check_health.sh` (44 s total), then burned **401 s** in a manual `for i in $(seq 1 40); sleep 10` loop waiting on a background baseline `pytest` run that had actually finished in `121.8 s`.
- **Turn 4 (`12:28 PM`, 248 calls, 22.9 min, 660 KB — hit tool-call limit #1):** Spent ~35 calls probing GitHub org repos and dispatching temporary Actions workflows to extract `ARENA_BRIDGE_TOKEN`, then attempted to split a 2,331-LOC `service.py` into 15 modules in one giant pass via `Bash` heredocs and Python string scripts before hitting the turn ceiling mid-flight with broken gates.
- **Turn 6 (`12:52 PM`, 146 calls, 13.2 min, 345 KB):** Cleaned up Phase 4 complexity/import-cycle issues, debugged a cache-hit regression in `funding_engine.study_cut`, ran full `pytest` 3 times (`161 s` + `179 s` + `203 s`), pushed `83d9c1d`, and synced Mac via bridge.
- **Turn 8 (`3:49 PM`, 250 calls, 163.7 min [2h 44m], 467 KB — hit tool-call limit #2):** Split Web Python + 6 frontend JS files (12 -> 18 ES modules) using regex scripts that dropped `$`, `state`, `activeStrategyName`, and `td()`. Because no static JS check existed and `/api/proof/catalog` took **181.5 s** per un-isolated browser launch, the agent spent **over 2.2 hours** in `nohup ... & sleep 285` loops (`ui_smoke`, `ui_probe2`, `diag1`..`diag12`, `smoke_win1`..`smoke_win6`) discovering four 1-line `ReferenceError` bugs one by one, plus building a `git worktree` at `HEAD` (`+360 s`) just to verify whether the 181 s catalog latency was a regression.
- **Turns 10 & 12 (`4:03 PM+`, 47 calls, 13.6 min, 133 KB):** Collected final smoke logs, tightened baselines, pushed `6b721ad` + `71c0411` + `fa4de0b`, and polled GitHub Actions CI with `sleep 45` + `sleep 150`.

---

## 2. Seven Root Causes Identified

### RC-1: Silent Live-Network Fallback in Synthetic Fixtures (`181.5 s` -> `<1.5 s` bug)
**Exact mechanism verified in codebase:**
1. `packages/strategies/src/moex_spread_lab/strategies/calendar/data_cache.py:287`:
   ```python
   def _is_offline(*flags: bool) -> bool:
       return (
           os.environ.get("MOEX_OFFLINE_ONLY") == "1"
           or "PYTEST_CURRENT_TEST" in os.environ
           or any(flags)
       )
   ```
2. `packages/web/tests/browser/*_smoke.py` are standalone Python scripts (not run under `pytest`, so `PYTEST_CURRENT_TEST` is unset).
3. `period_settings_smoke.py` sets `os.environ.setdefault("MOEX_OFFLINE_ONLY", "1")` at line 15, **after** other smoke scripts (`window_table_smoke.py`, `automatic_size_smoke.py`, etc.) or ad-hoc `/tmp/diag*.py` probes may already have imported modules or started without `MOEX_OFFLINE_ONLY=1` — and in every standalone `/tmp/ui_smoke*.py`, `/tmp/ui_probe*.py`, and `/tmp/diag*.py` script written by the agent, `MOEX_OFFLINE_ONLY` was **never set**.
4. Worse: even when `period_settings_smoke` is imported, if `MOEX_OFFLINE_ONLY` is unset in custom harnesses, `CalendarData._load()` (`calendar_spreads.py:181`) sees `offline = False` on first boot and calls `fetch_inputs(..., delay_seconds=0.05, opener=None)` across **all 26 `ASSETS` = 1,050 contract/spread `secid`s** (`contracts.py:133`) over a 6-thread pool! Every uncached contract sleeps `0.05 s` and fires a real HTTPS request to `https://iss.moex.com/iss/history/...`!
5. Simultaneously, `CalendarData(root)` in `ScreenerStore` (`web/store.py:62`) violates the own module docstring of `ProofService` (*"The screen is offline-only; it never downloads data"*): `CalendarData.__init__` defaults to allowing network fetches on cache miss unless `MOEX_OFFLINE_ONLY=1` or `PYTEST_CURRENT_TEST` happens to be set in `os.environ`!

**Impact:** Every browser smoke / diagnostic probe paid **~181.5 seconds** on `/api/proof/catalog` before rendering a single row, multiplying every trivial UI check by 3–5 minutes and causing **~130 minutes** of wasted runtime.

### RC-2: Missing Sub-Second Static Gate for Vanilla ES Modules (`static/js/*.js`)
Python files had `ruff` + `basedpyright` + `test_architecture_dag.py` (`<15 s`), catching undefined names immediately. Vanilla ES modules in `packages/web/src/moex_spread_lab/web/static/js/*.js` only had an acyclic import regex test (`test_frontend_es_module_import_dag_is_acyclic`), with **zero syntax, export/import resolution, or undefined-identifier checking**.
- When the agent split 6 JS files into 18 modules, four missing imports/helpers (`$`, `state`, `activeStrategyName`, `td`) slipped past `check_health.sh` and were only caught sequentially inside headless Chromium at ~5 minutes per cycle.
- Once the agent finally wrote `/tmp/js_check.py` (at line 25,023 of the log!), it caught missing symbols in **0.15 seconds** — **after** 90+ minutes had already been lost, and `/tmp/js_check.py` was left in `/tmp` instead of being committed into `check_health.sh`.

### RC-3: Oversized Multi-Phase Prompt Scope Without Hard Turn Checkpoints
Asking a single agent session to *"report status of `code-health-plan.md` and finish its implementation + push + sync Mac + restart server"* bundled **two major architectural splits** (`packages/proof` 2,331-LOC split + `packages/web` server & 18-module JS split + Phase 7 re-measurement) into one continuous context window.
- Once context passes ~150k–250k tokens (Turn 4 -> Turn 8), LLM latency per step rises, attention to earlier file contents degrades, and the agent hits platform per-turn tool limits (`250` calls), leaving uncommitted half-split states between turns.

### RC-4: Mechanical Refactoring via Ad-Hoc Regex String Replacement & `Bash` Heredocs
Instead of AST-aware extraction or structured file-edit tools, the agent performed 99 file writes via `python3 - <<'PY' ... s.replace(...)` and `cat > file <<'EOF'`, and 278 file reads via `sed -n` / `grep`.
- Regex import surgery caused duplicate `import { $ } from './dom.js';` lines (`L24118`), stripped `$` from `picker.js`, dropped `function td()`, and introduced `strategy-form.js <-> strategy-plans.js` cycles.
- Dumping raw file slices via `Bash` generated **555 KB** of stdout + **195 KB** of inline command text (~750 KB total context bloat).

### RC-5: Polling Anti-Patterns (`nohup ... & sleep 280`) Instead of Foreground `timeout` or Test Runner Scripts
To work around command timeouts or background execution, the agent repeatedly ran:
```bash
nohup uv run ... > /tmp/out.txt 2>&1 & sleep 285; tail /tmp/out.txt
```
- **Deterministic waste:** Even when a script failed with `AssertionError` after 22 seconds or finished in 121 seconds (`Turn 2: 401 s loop for a 121.8 s pytest`), the shell unconditionally slept for the full `280–401 s`.
- Furthermore, spawning 3–4 Chromium + `ThreadingHTTPServer` + Optuna processes simultaneously on a 2-vCPU sandbox caused CPU starvation and spurious Playwright click timeouts (`group_lamps_smoke` 8,000 ms click timeout at `L28051`), forcing yet another 280 s re-run.

### RC-6: Unresolved Workstation Bridge Bootstrap (`ARENA_BRIDGE_*` Secret Extraction Dance)
Because `ARENA_BRIDGE_PROMPT` and `ARENA_BRIDGE_TOKEN` were stored only as write-only GitHub Organization Secrets (unreadable via REST API from a cloud sandbox), the agent spent 9 minutes and **polluted `origin/main` history with 3 commits** (`f1f4d7b`, `96a15e2`, `036a8c8`) creating and deleting `.github/workflows/bridge-handoff.yml` just to exfiltrate the bridge URL and token via an Actions artifact.
- In addition, [`docs/development/remote-agent-testing.md`](remote-agent-testing.md) remains at `Status: Proposal` — so the agent still used raw `curl https://bridge.vernikr.bond/call` shell one-liners and hit process-tree kills when restarting the workstation screener with `nohup`.

### RC-7: Coarse Verification Pyramid (Running Full 200 s Gates Inside Inner Edit Loops)
The agent ran `check_health.sh` (`~20 s`), `packages/proof/tests` (`~160–180 s`), full `pytest` (`~205 s`), and all 11 Playwright browser suites (`11 x ~185 s`) inside inner debugging loops rather than enforcing a strict **0.2 s -> 3 s -> 18 s -> 200 s** verification pyramid.

---

## 3. Comprehensive Prevention Plan (Prioritized Engineering & Workflow Roadmap)

The roadmap is organized into **five concrete work packages (`P0`–`P4`)**, ordered by **time saved per hour of implementation effort**. Every item obeys repository constraints (`G1` numerical/hash parity, `G12` zero SaaS / zero committed secrets, zero runtime dependency bloat).

| Priority | Work Package | Effort | Expected Savings per Agent Session | Target Files |
|---|---|---:|---|---|
| **P0** | **Offline-by-default `CalendarData` & minimal catalog scope in synthetic/browser fixtures** | ~45 min | **Saves ~115–125 min** (`181.5 s -> <1.5 s` per `/api/proof/catalog` & browser smoke) | `packages/strategies/.../calendar/{calendar_spreads,data_cache}.py`, `packages/web/src/.../store.py`, `packages/web/tests/browser/` |
| **P1** | **Sub-second Frontend ES-Module Static Gate (`no-undef` / export-import / syntax)** | ~45 min | **Saves ~60–90 min** on any `packages/web` UI change (catches JS `ReferenceError`s in `<0.3 s` instead of Playwright) | `scripts/check_js_modules.py` (or `eslint` flat config via dev tool), `scripts/check_health.sh`, `packages/core/tests/test_architecture_dag.py` |
| **P2** | **Fast Browser Runner & Deterministic Process Wrapper (eliminate `nohup + sleep N`)** | ~40 min | **Saves ~25–40 min** of dead `sleep` padding and parallel 2-vCPU thrashing | `scripts/run_browser_smokes.py`, `.github/workflows/browser-smoke.yml`, `AGENTS.md` |
| **P3** | **Zero-Commit Workstation Bridge Handoff & `remote` CLI Implementation** | ~1.5–2 h | **Saves ~10–15 min/session** + eliminates temporary CI commits on `main` & orphan Mac processes | `docs/development/remote-agent-testing.md` (Phases 1 & 3), `AGENTS.md` |
| **P4** | **Session Scoping, Verification Pyramid & Tooling Guardrails in `AGENTS.md`** | ~20 min | **Prevents 250-call turn ceiling hits** and cuts context bloat by **50–70%** | `AGENTS.md`, task prompt templates |

---

### P0 — Fix the 181.5 s `/api/proof/catalog` Bottleneck & Enforce Offline-Only Server Runtime

**Goal:** Drop `/api/proof/catalog` on synthetic fixtures from **`181.5 s` to `< 1.0 s`** and drop each Playwright `*_smoke.py` script from **`~240–290 s` to `~4–8 s`**.

#### Root-cause fixes:
1. **Make `CalendarData` strictly offline-by-default (`allow_network: bool = False`):**
   - Currently `CalendarData._load()` and `CalendarData._load_asset()` (`packages/strategies/src/moex_spread_lab/strategies/calendar/calendar_spreads.py:159,181`) default to live MOEX ISS HTTP fetching (`opener=None, delay_seconds=0.05`) whenever `MOEX_OFFLINE_ONLY != "1"` and `PYTEST_CURRENT_TEST` is unset!
   - **Change:** Add `offline: bool = True` default on `CalendarData.__init__(self, root: Path, *, offline: bool = True)`. Only explicit intake/study CLI commands (`run_calendar_study` / `fetch_inputs`) may opt into network access. `ScreenerStore` and `ProofService` are documented offline-only views and must **never** trigger live HTTP requests or `0.05 s * 1,050` rate-limit sleeps on uncached contracts.
2. **Fast-skip missing cached files in offline `fetch_inputs()` without constructing URLs or thread pools:**
   - In `packages/strategies/src/moex_spread_lab/strategies/calendar/contracts.py:120` (`fetch_inputs`), when `opener is _offline` (offline mode), scan `os.scandir(cache_dir)` **once** to collect available `{secid}` prefixes matching `*-{HISTORY_FROM}-{till_date}-0.json` before looping over `1,050` theoretical `contract_chain()` symbols.
   - On synthetic fixtures (`two_calendar_windows` has only ~4 cached `SR*` files out of 1,050 symbols), intersecting `secids` with existing cache filenames reduces filesystem/pagination probes from **1,050 misses -> 4 hits** (`< 5 ms` total).
3. **Ensure every browser smoke script sets `MOEX_OFFLINE_ONLY="1"` before project imports:**
   - Add a tiny shared bootstrap helper `packages/web/tests/browser/_harness.py` (setting `MOEX_OFFLINE_ONLY="1"`, configuring `sys.path`, starting `ThreadingHTTPServer` with clean `ScreenerStore.close()` teardown so `TemporaryDirectory` never raises `Errno 39: Directory not empty` as seen at `L23358`, and setting sensible Playwright timeouts).
4. **Update stale caveat in `docs/development/phase7-backlog.md`:**
   - Remove the workaround note claiming `/api/proof/catalog` inherently takes ~185 s on synthetic fixtures once P0 lands.

**Acceptance Gate:**
- Benchmark `ScreenerStore(ScreenerInputs(root)).proof.catalog()` on `two_calendar_windows(root)` in a clean subprocess **without** `PYTEST_CURRENT_TEST` or `MOEX_OFFLINE_ONLY`: must complete in **`< 1.0 s`** with `0` outbound socket attempts.
- All 11 `packages/web/tests/browser/*_smoke.py` scripts complete sequentially in **`< 60–90 s` total** (down from `~45+ minutes`).

**Delivered (2026-11-22):**
- `CalendarData(root, *, offline=True)`: the offline contract is now a constructor default, not an
  environment variable. `_load`/`_load_asset` and the H1 candle path (`read_h1_candles`,
  `fetch_h1_candles`) honour it, so `ScreenerStore`, `ProofService` and the cohort worker pool can
  never open a MOEX ISS connection; only `run_calendar_study`/`fetch_inputs` may opt into network.
- `contracts.cached_secids()`: one `os.scandir` of the cache intersects the requested range with
  the 1,050 theoretical contract/spread secids, so offline `fetch_inputs` opens 4 cache files
  instead of building 1,050 urls, sleeping `0.05 s` each and firing 1,050 HTTP requests.
- `packages/web/tests/browser/_harness.py`: single bootstrap (`MOEX_OFFLINE_ONLY=1` + `sys.path`,
  `temporary_root()`, `serve()`/`serve_fixture()` with clean `store.close()` teardown, standard
  page/timeouts). All 11 smoke scripts now use it; the per-script `ThreadingHTTPServer` +
  `TemporaryDirectory` boilerplate and the late `setdefault("MOEX_OFFLINE_ONLY")` are gone.
- Evidence on the user's workstation (`/Users/vernikr/Downloads/projects/moex-spread-lab`,
  `f924663`): synthetic fixture catalog **0.15 s with 0 outbound socket connects** (traced);
  real-data cold `/api/proof/catalog` **~10 s -> 0.7-0.8 s warm**, of which ~3.4 s is local
  cache JSON parsing (`_load`) and ~3.0 s the 1h calendar catalog - no network involved, so
  P0 is done but a follow-up (persist the built catalog / build 1h lazily) would cut the cold
  first paint on real data.
- Measured on this Linux sandbox: cold `/api/proof/catalog` data path **181.02 s -> 0.05 s**;
  `window_table_smoke.py` **~240 s -> 16 s**. Guard: `packages/web/tests/test_offline_catalog.py`
  (default flag, cache-only offline fetch, warm catalog budget, clean-subprocess acceptance gate).
- Data-locality follow-up (workstation cache vs cloud agents, git/artifact transport) is recorded in
  [`data-locality.md`](data-locality.md); it does not change P1–P4.

---

### P1 — Sub-Second Static Verification for Frontend ES Modules (`static/js/*.js`)

**Goal:** Catch every missing import, unexported symbol, duplicate import, syntax error, and undefined identifier in `packages/web/src/moex_spread_lab/web/static/js/*.js` inside `./scripts/check_health.sh` in **`< 0.5 s`**, without requiring Playwright or npm runtime dependencies.

#### Implementation steps:
1. **Add `scripts/check_js_modules.py` (stdlib-only + optional `node --check` when `node` is present):**
   - **Exact export/import binding check:** Parse `import { a, b as c } from './mod.js'`, `export function/const/let/class`, and `export { x, y } from './other.js'` (including transitive re-exports) across `packages/web/src/moex_spread_lab/web/static/js/*.js`. Fail immediately if any imported symbol is not exported by its target module or if duplicate `import ... from './same.js'` headers exist.
   - **Syntax check (`node --check`):** When `shutil.which("node")` is available (present on workstation, GitHub Actions `ubuntu-latest`, and cloud sandboxes), run `node --check` over all 18 `.js` files in a single batched invocation (`~80 ms`) to catch syntax errors and duplicate lexical declarations (`Identifier '$' has already been declared`).
   - **Cross-module identifier guard:** Flag calls to any top-level symbol defined in another sibling `.js` module (such as `$`, `el`, `state`, `td`, `activeStrategyName`, `finishReview`) when the calling file neither defines nor imports that identifier locally.
2. **Wire into `./scripts/check_health.sh` and `packages/core/tests/test_code_health_gate.py`:**
   - Add `run_check js-modules uv run --no-sync python scripts/check_js_modules.py` as check #7 in `scripts/check_health.sh` (runs in parallel with `basedpyright`, adding **`0.0 s`** to the 12–18 s wall-clock gate).
   - Add unit tests proving that removing `$` from `picker.js` or `td` from `ledger.js` fails `check_js_modules.py` in `< 0.2 s`.

**Implementation note:** The dependency-free identifier guard is heuristic, not a full JavaScript scope analyzer. It checks sibling identifiers used as calls or member/index accesses; other undefined names and shadowing cases may escape it. Named imports/exports are checked; other ES module forms are not comprehensively resolved. Node parses every module in one process using `vm.SourceTextModule` (rather than `node --check`, which accepts only one file); syntax checking is explicitly skipped when Node is absent.

**Acceptance Gate:**
- Injecting any of the 4 Phase-5 regressions (`$` missing in `picker.js`, `state` missing in `ledger-cells.js`, `activeStrategyName` missing in `strategy-plans.js`, `td` missing in `ledger-cells.js`) fails `./scripts/check_health.sh` immediately with file + symbol name.

---

### P2 — Deterministic Browser-Smoke Runner & Elimination of `nohup + sleep` Loops

**Goal:** Eliminate dead `sleep 120..401` waiting loops and CPU-thrashing parallel Chromium spawns in 2-vCPU sandboxes.

#### Implementation steps:
1. **Create `scripts/run_browser_smokes.py`:**
   - Accepts `--filter <substring>` (e.g., `uv run --with playwright python scripts/run_browser_smokes.py --filter window_table`) or runs all 11 suites.
   - Launches **one shared Playwright `Browser` instance** across smoke modules where compatible (or caps concurrency at `--jobs 1` on `os.cpu_count() <= 2` and `--jobs 2` on `>= 4` cores), avoiding 11 cold Chromium startups and Optuna CPU contention.
   - Streams concise per-suite pass/fail timings (`[1/11] window_table_smoke.py ... PASS (5.4s)`) and prints tracebacks **only on failure** (preventing multi-thousand-line stdout dumps).
   - Reuse the exact same script in `.github/workflows/browser-smoke.yml`.
2. **Ban fixed-duration `sleep` polling in `AGENTS.md`:**
   - **Rule:** Never run `nohup <cmd> & sleep <N>` to wait on tests or probes. Run bounded commands synchronously with `timeout <max_sec> <cmd>` so execution returns the instant the process exits (e.g., at `2.1 s` on failure or `120 s` on pass, never waiting `285–401 s`), or use native background-process tools (`start_process` / `Bash` `run_in_background`) when available.

**Implementation status (2026-11-22):** The runner and CI integration are in place; standalone suites remain isolated in separate processes, with CPU-aware concurrency capped at one or two. The existing `AGENTS.md` rule already bans `nohup`/`sleep` polling. All 11 suites passed offline on a 2-vCPU sandbox with `--jobs 1` in 189.1 s; the <2 min target is not yet met there, and CI timing remains to be verified.

**Acceptance Gate:**
- `uv run --with playwright python scripts/run_browser_smokes.py` executes cleanly both locally and in `.github/workflows/browser-smoke.yml` within `< 2 min` (after P0).

---

### P3 — Frictionless Workstation Bridge Handoff & `remote` CLI (Eliminate Temporary CI Commits)

**Goal:** Never again spend 35 tool calls or commit temporary `.github/workflows/bridge-handoff.yml` probes to `main` to access the workstation bridge, and eliminate `nohup` process-tree kills on Mac screener restarts.

#### Observed residue (2026-11-22, evidence)

The retired `bridge-tmp` runs (`45004646222`, `45004704247`) left the `bridge-handoff` artifact
(`prompt.md` + `token.txt`) downloadable for any repo writer until its 24 h expiry; that was the
only way to recover the bridge token from a cloud session, because org secrets are write-only by
design. Consequences to enforce: (a) treat any Actions artifact that ever held a credential as
**burned** and rotate the bridge token after use; (b) never let a handoff workflow upload a token
again — the URL belongs in an org *variable* and the token is pasted into the prompt or re-derived
locally; (c) check for and delete stale `bridge-handoff` artifacts after any bridge session.

#### Operational & technical steps:
1. **Credential delivery protocol (Zero GitHub Actions exfiltration):**
   - **Option A (Recommended for Arena / Cloud Agents):** Keep a single local helper on Mac (`arena-bridge prompt` or an alias `msl-agent-header`) that copies the 2-line runtime header (`ARENA_BRIDGE_URL=https://bridge.vernikr.bond` + ephemeral `ARENA_BRIDGE_TOKEN=...`) into the opening prompt whenever workstation sync is requested, **or** configure `ARENA_BRIDGE_URL` as a non-secret GitHub Org Variable and pass only the short-lived `arena-bridge` session token in the prompt.
   - **Hard prohibition in `AGENTS.md`:** Agents must **never** commit temporary GitHub Actions workflows to `main` (or any branch) to dump or relay repository/organization secrets into Actions artifacts. If workstation bridge credentials are required for a post-push sync step and are not present in runtime environment variables or the user prompt, the agent completes the `origin/main` push and asks the user for the bridge token or runs the sync once provided.
**Implementation evidence (2026-11-22):** The launchd agent is installed on the Mac and macOS Background Items identifies it as `moex-spread-lab-screener`; `remote status`, `sync`, `probe`, and `bench` are implemented. Isolated catalog A/B tests on the workstation matched byte-for-byte (SHA-256 over canonical full JSON), including sealed 1h data, and temporary worktrees/processes were cleaned. Bridge artifact inventory had no `bridge-handoff` matches. Warm catalog probes were about 0.3–0.5 s inside the server; the original measured cloud-to-workstation round trip was 2.18–2.57 s. Avoiding unrelated CLI plugin imports produced three warm catalog CLI probes of 1.257, 1.210 and 1.402 s; this is a small sample, not a cold-start or load guarantee. Current limitations and safety trade-offs (copied data, partial telemetry, initial rather than Phase-2-complete suites) are documented in `remote-agent-testing.md`.

2. **Implement Phases 1, 3, and 4 of [`docs/development/remote-agent-testing.md`](remote-agent-testing.md):**
   - **`launchd` service on Mac (`local.moex-spread-lab.screener`):** Manages the canonical workstation screener process cleanly so restarting after `git pull --ff-only` is a single `launchctl kickstart -k gui/$(id -u)/local.moex-spread-lab.screener` (never killed when an Arena Bridge `bash` call exits).
   - **`moex-spread-lab remote sync` & `remote status`:** Single deterministic CLI command replacing fragile multi-step `curl -X POST https://bridge.../call` JSON-escaped shell strings.
   - **`GET /api/dev/health` & `POST /api/dev/probe` (`response_mode="summary"`):** Returns `< 2 KB` JSON summaries instead of dumping megabytes of ledger/series JSON into agent context.

---

### P4 — Agent Operating Protocol: Turn Budgets, Verification Pyramid & Edit Hygiene (`AGENTS.md`)

Even with fast tests, unconstrained agent behavior on large tasks causes context rot and 250-call ceiling crashes. Enforce four operational rules in `AGENTS.md` and task prompts:

#### 1. Strict Per-Turn Scope Cap ("One Cohesive Slice = One Commit & Push")
- **Rule:** Never combine two multi-file architectural phases (e.g., `packages/proof` split + `packages/web` split) in a single turn or uncommitted working tree.
- **Hard budget per turn:** Target **`<= 60` tool calls** and **`<= 15 minutes`** wall-clock per slice. If a task spans multiple packages or >5 files:
  1. Decompose into self-contained slices (`Slice A`, `Slice B`, ...).
  2. Complete `Slice A` -> pass fast gates -> commit & push to `main` -> report concise status.
  3. Proceed to `Slice B` only if within budget, or checkpoint cleanly for the next turn (zero half-done edits stranded at the 250-call limit).

#### 2. Four-Stage Verification Pyramid (Never Run Heavy Suites in Inner Loops)
Agents must run checks strictly in order of cost, advancing only when the cheaper tier is `100%` green:

| Stage | Command | Typical Latency | When to Run |
|---|---|---:|---|
| **Stage 1: Targeted file lint/type/JS check** | `uv run --no-sync ruff check <touched_paths>` + `uv run --no-sync basedpyright <touched_paths>` + `python scripts/check_js_modules.py` | **`0.3 – 2.5 s`** | After **every** edit batch during implementation/debugging. |
| **Stage 2: Focused unit test file** | `uv run --no-sync pytest packages/<pkg>/tests/test_<target>.py -q` | **`1 – 10 s`** | After logic or module-boundary changes in `<pkg>`. |
| **Stage 3: Workspace health gate + package suite** | `./scripts/check_health.sh && uv run --no-sync pytest packages/<pkg>/tests -q -n auto --dist loadfile` | **`18 – 45 s`** | **Once** when the slice is feature-complete. |
| **Stage 4: Pre-push full gate / browser smoke** | Full `pytest -q -n auto --dist loadfile` (`~120–200 s`) + `scripts/run_browser_smokes.py --filter <affected>` (only if `packages/web` or `/api/proof` changed) | **`60 – 200 s`** | **Once** immediately before `git commit && git push`. |

#### 3. Context-Window & Edit Hygiene (Eliminate `Bash` Read/Write Bloat)
- **Prefer native `Read` / `Edit` / `MultiEdit` / `Grep` / `Glob` tools** over `Bash` (`sed -n`, `cat`, `cat <<EOF`, inline `python3 - <<PY` string surgery) whenever the agent runtime provides them.
- **When splitting a module (Python or JS), verify symbol conservation mechanically before running any test:**
  - Before deleting functions/exports from a monolith `M` into `M_1..M_k`, assert that the set of exported/public symbols of `M` (or its re-export facade) is a superset of `HEAD`'s symbols, and run Stage 1 (`ruff` + `basedpyright` + `check_js_modules.py`) immediately.
- **Cap command output:** Never dump full `pytest` progress dots, raw JSON payloads, or entire >300-LOC files to `Bash` stdout (`--tb=short -q`, pipe noisy installers to log files and print `tail -n 20` only on non-zero exit).
- **Use `repowise` MCP on workstation / pre-generated `phase7-backlog.md` tables:** Read exact hotspot/coupling summaries first instead of grepping across 150+ files to rediscover module boundaries.

#### 4. Prompt Template for Future Multi-Step Tasks
When assigning large backlog items (such as [`phase7-backlog.md`](phase7-backlog.md) `F2`, `F3`, `F4`, `F6`), structure prompts with explicit slice boundaries:

```text
Выполни только пункт <F3 / один подэтап F6> из docs/development/phase7-backlog.md.
Ограничения на заход:
1. Один атомарный срез (не более 1 пакета / 3–5 файлов за коммит).
2. Во время правок гоняй только точечные проверки (<5 с), полный check_health.sh и релевантный пакетный pytest — один раз перед коммитом.
3. Не используй sleep-циклы и не коммить временные workflow.
4. По завершении среза: коммит, пуш в origin/main, краткий отчёт. Если остались следующие срезы — перечисли их и остановись.
```

---

## 4. Implementation Checklist & Expected End-State

- [x] **Step 1 (`P0` — Offline `CalendarData` & fast catalog fixture, delivered 2026-11-22):** Make `CalendarData` offline-by-default (`offline: bool = True`), pre-filter `fetch_inputs` by existing cache filenames in offline mode, add `packages/web/tests/browser/_harness.py` (`MOEX_OFFLINE_ONLY="1"` + clean `store.close()`). Verify `/api/proof/catalog` `< 1.0 s` on synthetic fixtures.
- [x] **Step 2 (`P1` — `scripts/check_js_modules.py`):** Enforce static export/import resolution, `node --check` syntax validation, and cross-module undefined-symbol detection on `static/js/*.js` inside `./scripts/check_health.sh`.
- [ ] **Step 3 (`P2` — `scripts/run_browser_smokes.py`):** Runner, `--filter`, and CI wiring delivered; <2 min acceptance remains open (189.1 s on 2-vCPU sandbox; CI timing not yet verified).
- [ ] **Step 4 (`P4` — `AGENTS.md` guardrails):** Add concise rules for the 4-stage verification pyramid, ban `nohup + sleep` polling and temporary secret-extraction CI workflows, and enforce single-slice per-turn checkpoints (**applied in initial form alongside this plan**).
- [ ] **Step 5 (`P3` — Workstation `remote` CLI & `launchd` daemon):** Core daemon, CLI (including isolated A/B worktrees) and probe endpoints are delivered. Remaining: per-request worker peak RSS instrumentation, broader end-to-end latency verification, and verified credential rotation after the retired handoff artifacts. See [`remote-agent-testing.md`](remote-agent-testing.md) for implemented suite and isolation limitations.

### Projected Impact on Equivalent Future Work

| Metric | Observed Session (2026-11-22) | Target After P0–P4 | Improvement |
|---|---:|---:|---:|
| `/api/proof/catalog` on synthetic fixtures | `181.5 s` | **`< 0.5 s`** | **>350x faster** |
| Catching a missing JS import (`$`, `td`) | `~25–30 min` (Playwright timeout loop) | **`< 0.3 s`** (`check_health.sh`) | **>5,000x faster** |
| All 11 Playwright browser smoke suites | `~45–60 min` (with retries/contention) | **`< 60–90 s`** | **~35–45x faster** |
| Total `Bash` calls per completed slice | `248–250` (hitting hard limit) | **`25–55`** | **~5x fewer calls** |
| Transcript / context footprint per slice | `470–660 KB` | **`< 80–120 KB`** | **~5x smaller context** |
| Wall-clock time for a Phase-4 / Phase-5 split | `~4.5 hours` (6 turns) | **`~20–35 min` (2 clean turns)** | **~8–10x faster** |


**P3 verification update (2026-11-22):** A real three-worker 1h calendar
probe followed by launchd restart left none of the old server's four child
processes alive, and the new server returned HTTP 200. Five warm cloud catalog
CLI probes had median end-to-end latency 1.165 s (range 1.101–1.782 s); cold
startup is slower. The initial single-proof benchmark returned a -0.01 RUB
ledger reconciliation on its 1d case, so zero-reconciliation acceptance is
not established. The initial window and optimize suites are smaller than the
proposed canonical workloads; see the workstation verification note in
`remote-agent-testing.md`.
