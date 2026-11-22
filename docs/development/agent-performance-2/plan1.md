# Agent Task-Completion Latency Optimization Plan (Generation 2)

Status: **Actionable architectural & engineering plan** (2026-11-22, forensic post-mortem of three post-`P0` agent transcripts: `fb9e1ee.md`, `b687ae2.md`, and `9dae286.md`, evaluated against `origin/main` at `e12bb51`).

---

## 1. Executive Summary & Cross-Session Forensic Post-Mortem

Generation 1 of [`agent-performance-plan.md`](../agent-performance-plan.md) (`P0`–`P4`) eliminated the catastrophic **181.5 s live MOEX ISS network fallback** in `CalendarData` and added static ES-module checks (`check_js_modules.py`), browser-smoke orchestration (`run_browser_smokes.py`), and `moex-spread-lab remote` workstation tooling.

Analysis of the **three subsequent real-world agent sessions** (`685` total `Bash` calls, **`166.5 minutes` (`9,989.7 s`) of active shell execution**, **`2.58 MB` of exported transcript**, and **`38` commits** across `P1–P3`, `P7.1`, and `P7.2`) reveals a clear **second-generation bottleneck regime**:

> **Core finding:** Network stalls (`P0`) are gone, yet **69%–81% of all active execution time** is now consumed by **redundant full-workspace test and static-analysis runs** (`full pytest` + full `check_health.sh`) repeated after every micro-slice — amplified by **human-in-the-loop turn fragmentation** (`10–15` manual `"продолжи"` interventions per multi-file task), **monolithic slow equivalence tests inside default `pytest` discovery**, **12x cold Chromium/fixture startups in browser smokes**, and **full `data/` + `data-lake/` directory copies on every `remote bench` worktree run**.

### 1.1 Cross-Session Quantitative Comparison

| Metric | Session A: `fb9e1ee.md` (`P1` + `P2` + `P3`) | Session B: `b687ae2.md` (`P7.1`) | Session C: `9dae286.md` (`P7.2` partial) | Combined / Contrast Insight |
|---|---:|---:|---:|---|
| **Task scope** | Implement `P1`, `P2`, `P3` + rounding fix | Implement `P7.1` (F3 drop-in + F4 lazy registry) | Implement `P7.2` (`packages/proof` split + typing) | `b687ae2` is the control benchmark for single-turn execution |
| **Messages / Assistant turns** | `41` msgs (`20` assistant turns) | **`2` msgs (`1` assistant turn)** | `26` msgs (`13` assistant turns; `1` aborted) | **30 manual user nudges** across Sessions A & C vs **0** in B |
| **Wall-clock duration** | **~5h 56m** (`17:47 -> 23:43`, incl. ~2h idle gap) | **~13–15 min** (`5.8m` shell + `6.6m` thinking) | **~45–55 min** (`30.3m` shell + `10x` `"продолжи"` waits) | Single-turn `P7.1` finished **4–15x faster** per unit of scope |
| **Commits pushed to `main`** | `24` commits (`24` pushes + `24` Mac syncs) | `2` commits (`1` push + `1` Mac sync) | `12` commits (`12` pushes + `12` Mac syncs) | Every extra push-per-micro-slice triggered full verification tax |
| **Total `Bash` calls (errors)** | `397` (`49` non-zero exits = `12.3%`) | `127` (`4` non-zero exits = `3.1%`) | `161` (`10` non-zero exits = `6.2%`) | `685` total `Bash` calls (`63` failed = `1,026 s` wasted) |
| **Structured `Read`/`Edit` calls** | `3` reads, **`0` edits** (`181` Python/heredoc writes) | `0` reads, `4` edits (`35` Python/heredoc writes) | **`0` reads, `0` edits** (`65` Python/heredoc writes) | **`98.6%` of edits** still bypassed structured tools via `Bash` |
| **Active shell runtime** | **`130.3 min`** (`7,819.6 s`) | **`5.8 min`** (`349.3 s`) | **`30.3 min`** (`1,820.8 s`) | **`166.5 min` (`9,989.7 s`)** total active shell wait |
| **1. Full-workspace `pytest`** | **`76.8 min`** (`4,605 s`, `25` calls = **`58.9%`**) | **`3.2 min`** (`189 s`, `2` full runs = **`54.2%`**) | **`16.5 min`** (`988 s`, `12` runs = **`54.3%`**) | **#1 bottleneck in all 3 logs (`57.9%` of total shell time)** |
| **2. Full `check_health.sh` + `code_health_gate` + `basedpyright`** | **`29.6 min`** (`1,777 s`, `70` calls = **`22.7%`**) | **`0.5 min`** (`31 s`, `6` calls = **`9.0%`**) | **`9.2 min`** (`552 s`, `69` calls = **`30.3%`**) | **#2 bottleneck (`23.6%` of total shell time)** |
| **3. Workstation bridge (`sync`/`probe`/`bench`)** | **`12.6 min`** (`758 s`, `105` calls = **`9.7%`**) | **`0.4 min`** (`24 s`, `7` calls = **`7.0%`**) | **`1.0 min`** (`58 s`, `15` calls = **`3.2%`**) | Heavy `uv sync` on every sync + full `data/` copy on bench |
| **4. Focused `pytest`** | **`5.0 min`** (`300 s`, `32` calls = **`3.8%`**) | **`1.3 min`** (`76 s`, `14` calls = **`21.7%`**) | **`3.0 min`** (`182 s`, `12` calls = **`10.0%`**) | Healthy fast feedback (`0.3–30 s`), dwarfed by full-suite runs |
| **5. Browser smokes (`Playwright`)** | **`4.1 min`** (`249 s`, `15` calls = **`3.2%`**) | `0.0 min` (`0` calls) | `0.0 min` (`0` calls) | `189.1 s` for 11 smokes on 2 vCPUs (missed `<120 s` P2 gate) |
| **6. `Bash` `sed`/`cat`/`grep` reads** | `1.2 min` (`72 s`, `111` calls -> **`472 KB`**) | `0.1 min` (`5.5 s`, `62` calls -> **`251 KB`**) | `0.1 min` (`7.5 s`, `35` calls -> **`249 KB`**) | **#1 source of context bloat (`973 KB` = `47.0%` of tool text)** |
| **Transcript size** | `1,354 KB` (`22,268` lines) | `602 KB` (`10,757` lines) | `664 KB` (`13,077` lines) | `2.62 MB` (`46,102` lines) across 3 sessions |

---

## 2. Ten Systemic Bottlenecks Identified in the Logs (With Exact Code & Log Evidence)

### B1. The "Micro-Slice Tax" & Over-Interpreted Single-Slice Stop Rule (`58%` of Shell Time + 22 Manual `"продолжи"` Prompts)

**Evidence in `9dae286.md` (`P7.2`) and `fb9e1ee.md` (`P3`):**
- In `9dae286.md`, the agent decomposed `P7.2` into **12 tiny 40–150 LOC helper extractions** (`9dae286`, `72640fe`, `2cfca9f`, `39c7ea4`, `1959971`, `ce46f4c`, `ad48af1`, `fbcf106`, `b1f480d`, `3024a85`, `ee7e374`, `e12bb51`).
- Because `P4` in `agent-performance-plan.md` told agents *"One Cohesive Slice = One Commit & Push ... If remaining slices exist — list them and stop"* **and** the user prompt said *"после каждого захода: пуш в main, обновить проект в /Users/vernikr..., перезапустить сервер"*, the agent **halted after every ~100-line extraction** and waited for a human `"продолжи"` **11 times in a row**!
- Even worse, **every single 100-line internal extraction inside `packages/proof`** paid the **entire repository-wide pre-push ceremony**:
  1. `code_health_gate.py --update-baseline` (`~15 s`)
  2. `./scripts/check_health.sh` (`~15–17 s` on 4 vCPUs; `~31–38 s` on 2 vCPUs)
  3. **Full 6-package `pytest -q -n 4 --dist loadfile`** (`79–89 s` on 4 vCPUs; **`175–338 s`** on 2 vCPUs!)
  4. `git push origin main` (`~1.6 s`)
  5. `moex-spread-lab remote sync` + `remote status` (`~5–7 s`, running `uv sync --frozen` + `launchctl kickstart -k` on the user's Mac **12 times in 45 minutes**, dropping warm server caches every 3 minutes!).
- In `9dae286.md`, editing **only `packages/proof`** (`0` lines changed in `data`, `accounting`, `strategies`, and only 1 re-export check in `web`) triggered **12 full-workspace `pytest` runs (`988 s`)**, re-running `core`, `data`, `accounting`, `strategies`, and `web` suites **12 times unnecessarily**.
- In `fb9e1ee.md`, the agent ran **full-workspace `pytest` 25+ times (`4,605 s` = 76.8 minutes!)** — including **five sequential single-worker runs (`pytest -q` without `-n auto`, taking `263 s`, `293 s`, `338 s`, `267 s`, `155 s` = `21.9 minutes` in Calls `#264, #276, #282, #299, #394`)**, four of which were triggered **immediately after editing only Markdown documentation** (`docs/development/remote-agent-testing.md`)!

**Contrast (`b687ae2.md`):** The agent implementing `P7.1` worked autonomously in **1 turn** (`0` human interruptions), used fast targeted tests (`0.14 s – 3.36 s`) during development, ran full `pytest` at the end (`84 s`), pushed once, and synced the Mac once — finishing in **5.8 minutes of shell time**.

---

### B2. Package-Blind Test Execution & Unseparated Heavy Stress/Equivalence Suites (`145–338 s` per Run)

**Evidence in codebase & logs:**
1. **No dependency-aware test runner exists:** Although the workspace is a clean 6-layer DAG (`core -> data|accounting -> strategies -> proof -> web`), agents have no `scripts/verify_slice.py --changed` command that maps `git diff` to affected downstream packages (or skips `pytest` entirely when only `docs/*.md` changed!).
2. **`test_perf_equivalence.py` (`1,285` LOC) runs inside every default `pytest` invocation:**
   - Inside `packages/proof/tests/test_perf_equivalence.py`, 9 end-to-end equivalence tests build full H1 + 1d workspaces (`_build_h1_workspace` called **3 times** in fresh tempdirs, `fixture()` called **6 times**), spawn real OS `multiprocessing` `spawn` worker pools (`CohortWorkerPool` with `spawn` interpreter startup), run **18 full Optuna `research_run` cohorts**, and execute **every cohort twice** in `test_fastcore_c_matches_python_fallback_bit_for_bit` (once with C99 `_fastcore`, once with pure-Python fallback!).
   - In CI (`.github/workflows/ci.yml`), `perf-equivalence` is already a separate job, **yet `package-tests (proof)` and local `uv run pytest` also run `test_perf_equivalence.py` every single time**, accounting for `~26–55 s` of wall-clock tail latency (`xdist --dist loadfile` pins one worker to `test_perf_equivalence.py` long after other files finish).
3. **Redundant per-test fixture reconstruction across `packages/proof/tests`:**
   - `fixture(root)` and `_build_h1_workspace(root)` serialize dozens of JSON files (`lqdt-lineage.json`, `current-intake.json`, `ruonia`, `SR*` cache files, `*_1h.json`) from scratch **dozens of times per test run** (`10x` in `test_pair_plugin_pipeline.py`, `10x` in `test_proof_search.py`, `6x` in `test_perf_equivalence.py`) instead of cloning a `session`-scoped immutable template directory via `shutil.copytree` (or hardlinks) in `< 2 ms`.
4. **Subprocess workspace-copy tax in `packages/core/tests/test_architecture_dag_dropin.py` (`Call #13`: `45.3 s` for `core` tests on 2 vCPUs):**
   - `test_architecture_dag_dropin.py` copies **every Python file in `src/` and `packages/*/src/` twice** into `tmp_path` (`_copy_workspace_sources`) and spawns **3 separate Python subprocesses** running full AST parsing (`_run_dag_test`), even though `test_architecture_dag.py` already parameterizes `REPO_ROOT` / `PACKAGES_DIR` and could be tested in-memory or against a lightweight synthetic overlay in `< 50 ms`!

---

### B3. Full-Workspace Static Gate (`check_health.sh`) Re-Executed 69 Times (`30–38 s` Each on 2 vCPUs)

**Evidence in `fb9e1ee.md` (`62` invocations, `26` failures) & `9dae286.md` (`18` check_health + `33` `code_health_gate.py` + `21` `basedpyright` = `72` invocations):**
1. **`check_health.sh` has no `--fast` / `--changed` mode and spawns 7 concurrent `uv run` processes on 2 vCPUs:**
   - Launching `ruff check`, `ruff format`, `basedpyright` (Node.js Pyright engine analyzing all 180+ files), `vulture`, `code_health_gate.py` (which itself spawns `complexipy` over the entire repo + `lizard` over `packages/` & `src/`), `architecture_gate.py` (which parses ASTs of all 180+ modules + runs `validate-manifest` + `secret-scan` over all tracked files), and `check_js_modules.py` (spawning another Node.js process) **simultaneously on a 2-vCPU container** creates `7–9` competing heavy processes, stretching wall time from `13 s` (4+ cores) to **`30–38 s`** (2 vCPUs) — and **`104 s`** when overlapping with background work (`b687ae2.md` Call `#115`).
2. **`check_health.sh` dumps all passing logs before/around failures and doesn't fail-fast:**
   - Lines `30–40` of `scripts/check_health.sh` `cat "$logs/$name.log"` unconditionally for all 7 checks (`=== ruff-format: passed === 352 files already formatted ...`), dumping `1.5–3 KB` of noise on every call (`223 KB` across the logs) and obscuring the 2-line error that actually failed (`fb9e1ee` Calls `#12, #16, #67, #68, #73, #86, #96`).
3. **`code_health_gate.py` re-scans the entire repository on `--update-baseline` (`33` calls in `9dae286.md`, `15–17 s` each):**
   - During `P7.2`, the agent ran `code_health_gate.py` 33 times (`177 s`) plus `check_health.sh` 19 times (`322 s`) — running `complexipy` and `lizard` **52 times** over untouched packages.

---

### B4. Architectural Friction blocking `basedpyright` Strict Mode on PEP 420 Namespace Packages (`6,352` Errors Discovered in `9dae286.md`)

**Evidence in `9dae286.md` Turn 26 & `modular-architecture-review.md:115–123`:**
- When the `P7.2` agent attempted to enable `[tool.basedpyright].strict` on `packages/{data,accounting,strategies,proof}/src` as requested by `modular-architecture-review.md`, `basedpyright` exploded with **`6,352` diagnostics** (`3,675` in `proof` alone), including **`327` `reportMissingImports` false positives** caused by PEP 420 implicit namespace packages (`moex_spread_lab` split across 7 `extraPaths` roots: `src/` + `packages/*/src`).
- Because `strict = [...]` in Pyright/basedpyright **re-enables `reportMissingImports = "error"` inside strict directories** (overriding top-level `reportMissingImports = "none"` in `pyproject.toml:222`!) AND enables `reportUnknownParameterType` / `reportUnknownMemberType` / `reportUnknownVariableType` across `dict[str, Any]` boundaries, any agent asked to "turn on `strict`" hits a brick wall, wastes turns running diagnostic experiments, and has to abort strictness.
- Similarly, in `fb9e1ee.md`, `packages/core/src` being in `strict` mode caused **11 separate `basedpyright` failures** (`reportUnknownVariableType` on `json.load()`, `reportDeprecated` on `@contextmanager -> Iterator[T]`, `reportUnknownMemberType` on untyped dicts) when writing `remote_cli.py` and `workstation_bench.py`, each costing a 30–38 s `check_health.sh` round-trip because the agent didn't run `basedpyright <single_file>` or have typed JSON helpers in `msl-core`.

---

### B5. Manual Layer-Table Registration (`_LAYER_SPECS`) & CLI Help-Hash Friction on Every New Module

**Evidence in `fb9e1ee.md` Calls `#67, #68, #295`:**
1. In `packages/core/tests/test_architecture_dag.py:30–55`, `proof_runtime.*`, `web.*`, and `strategies.<plugin>.*` are auto-classified from disk via `.glob()`, **but `core.*`, `data.*`, `accounting.*`, and `strategies` root modules are still hardcoded strings in `_LAYER_SPECS`!**
   - Every time an agent extracts a helper module in `packages/core` (`remote_cli`, `workstation_remote`, `workstation_bench`, `cache_metrics`, `phase_metrics`), `packages/data`, or `packages/accounting`, `test_every_package_module_has_explicit_layer_assignment` **fails** (`Unclassified modules: ['core.remote_cli', 'core.workstation_remote']`, Call `#68`, wasting `74 s` + `76 s` in Call `#295`!).
   - Worse: because `_LAYER_SPECS` is a single shared string in `packages/core/tests/test_architecture_dag.py` (`297` LOC, under baseline ratchet!), editing it collides across branches and risks tripping the token/LOC ratchet on `test_architecture_dag.py` itself!

---

### B6. Ephemeral Cloud Sandbox Cold-Start & Mid-Session Reset Penalty (`git clone` + `pip install uv` + `uv sync` + `_fastcore.c` Compile)

**Evidence across all 3 logs (`fb9e1ee` Calls `#3, #9, #306–307`; `b687ae2` Calls `#2, #14, #15`; `9dae286` Calls `#2, #10`):**
- Arena cloud sandboxes start **completely empty** (`/home/user` has no repo, no `uv` binary, no `.venv`, no compiled `_fastcore.so`, and no `git` credentials configured).
- Worse, in `fb9e1ee.md` (between Turn 26 at `8:20 PM` and Turn 28 at `10:53 PM`), the container hibernated/reset: `uv` vanished from `~/.local/bin`, `git remote origin` disappeared, and executable bits `100755 -> 100644` flipped on `scripts/*.sh`, forcing the agent to spend 4 calls (`#304–307`) diagnosing dirty file modes, reinstalling `uv`, re-running `uv sync --frozen`, and re-adding `origin`.
- Across sessions, agents repeatedly fumble git auth (`exit 128` in `fb9e1ee` Calls `#20, #22, #198, #200` when credential helper wasn't set or expired; credential-in-URL anti-pattern in `b687ae2` Call `#2`), `PATH` loss (`export PATH="$HOME/.local/bin:$PATH"` repeated **40+ times** because `uv` installs to `~/.local/bin` which isn't in non-login shell `PATH`), and parsing `arena-promt.md` via inline regex (`export ARENA_BRIDGE_URL=$(python -c "import re...")` repeated **25+ times** in `fb9e1ee` and `14 times` in `9dae286`, failing twice when the file path changed from `arena-promt.md` to `arena-promt-2.md` in Calls `#18, #20`!).

---

### B7. Workstation `remote bench` Copies Entire `data/` & `data-lake/` + Runs `uv sync` on Every Benchmark (`22–52 s` per Call)

**Evidence in `packages/core/src/moex_spread_lab/core/workstation_bench.py:103–131` & `workstation_remote.py:68`:**
1. **`workstation_bench.py:_copy_data()` performs a full physical `shutil.copytree` of `data/` and `data-lake/` (hundreds of MB of JSON cache + Parquet lake) PLUS `uv sync --frozen` inside every disposable `candidate_worktree`!**
   - Look at `remote-agent-testing.md` §3 vs what was actually coded in `workstation_bench.py`: the design specified zero-copy read-only access, **but the implementation copied the entire `data/` and `data-lake/` trees on disk** (`_copy_data`) and spawned a fresh cold `uv sync --frozen` + cold server (`~8.0 s` startup) on **every single `remote bench` call** (`52 s` in Call `#180`, `35 s` in Call `#268`, `34 s` in Call `#343`, `29 s` in Call `#172`, `24 s` in Call `#189`, `22 s` in Calls `#231, #278`).
   - Furthermore, on macOS APFS (`~/Downloads/projects/moex-spread-lab`), `cp -cR` (APFS copy-on-write clonefile) clones 500 MB of files in **`< 30 ms` with zero disk space and full mutation isolation**, whereas Python `shutil.copytree` copies every byte (`shutil.copy2`)! And when `patch` does not touch `pyproject.toml` / `uv.lock`, running `uv sync --frozen` inside a worktree is completely unnecessary if `PYTHONPATH` points to the candidate `packages/*/src` over the existing `.venv/bin/python3`!
2. **`workstation_remote.py:sync()` unconditionally runs `uv sync --frozen` + spawns a Python subprocess to check `_fastcore` + restarts `launchd` on every commit (`5.5–7.0 s`):**
   - Even when `changed` is `["docs/development/plan.md"]` or pure `.py` edits with zero `uv.lock` / `pyproject.toml` / `_fastcore.c` changes, `remote sync` runs `uv sync --frozen` AND `launchctl kickstart -k` (destroying the workstation server's warm `CalendarData` and `CohortWorkerPool`, so the user's next click in the browser pays a **7–10 s cold catalog/pool penalty**!).
3. **`remote_cli.py:status` makes 2 sequential HTTPS round-trips to Arena Bridge (`/health` then `/exec`), and `sync` + `status` is 3 round-trips:**
   - Every post-push handoff runs `remote sync` followed by a separate `remote status` (waiting for `launchd` HTTP 200), often hitting HTTP `None` race conditions right after `kickstart -k` (`Call #136: HTTP 000 exit 7`) because `sync()` returns before polling loopback readiness locally on the Mac!

---

### B8. Browser Smoke Runner (`189.1 s` on 2 vCPUs) Launches 12 Cold Chromium Instances, 12 Servers & 43 Full Optuna Runs

**Evidence in `scripts/run_browser_smokes.py` & `packages/web/tests/browser/*_smoke.py` (`fb9e1ee.md` Call `#36`):**
- `P2` aimed for `< 120 s` across all browser smokes, yet `run_browser_smokes.py` took **`189.1 s`** (and there are now **12** `*_smoke.py` files after `rounding_display_smoke.py`).
- Why? `run_browser_smokes.py` runs each `*_smoke.py` in a **brand-new Python subprocess** (`subprocess.run([sys.executable, str(path)])`), which:
  1. Re-imports the entire Python/DuckDB/PyArrow/Optuna/Playwright stack **12 times** (`~1.5 s x 12 = 18 s`);
  2. Rebuilds synthetic JSON fixtures on disk (`fixture(root)`) **12+ times** (`period_settings_smoke` even builds multiple fixtures);
  3. Starts and tears down `ThreadingHTTPServer` + `ScreenerStore` **12+ times**;
  4. Launches and closes a **cold headless Chromium browser process (`pw.chromium.launch()`) 12 times** (`~2.5–3.5 s x 12 = 35 s`);
  5. Runs full **80-trial-per-family Optuna cohort searches** (`TRIALS = 80`, up to 240–320 trials per click of `#auto-tune`!) **dozens of times** (`checkbox_tuning_smoke.py`: `32.3 s`; `strategy_search_smoke.py`: `26.5 s`; `global_capital_smoke.py`: `20.6 s`; `group_lamps_smoke.py`: `20.2 s`) just to test UI DOM state transitions (`#compatibility` modal, checkbox indeterminate state, lamp rendering, seed badge), which behave identically with `MOEX_BROWSER_SMOKE_TRIALS=12` (except one dedicated numerical assertion if any)!

---

### B9. Context Bloat & Fragile File Surgery via `Bash` (`973 KB` of `sed`/`cat`/`grep` + `281` Inline Python/Heredoc Edits)

**Evidence across all 3 logs:**
- Even after `AGENTS.md` added *"prefer structured Read/Edit/Grep tools over Bash sed/cat <<EOF dumps"*, Arena's agent environment either did not expose structured `Edit` to the model in `fb9e1ee` / `9dae286` (`0` `Edit` calls!) or the model defaulted to `Bash` (`685` `Bash` calls vs `4` `Edit` calls total across all 3 sessions!).
- Reading files via `sed -n '1,260p' ...; sed -n '250,500p' ...` generated **`972,626` characters (`~270k` tokens — `47%` of all tool I/O)** across the 3 logs.
- Editing files via `python - <<'PY' s=open(p).read(); a=s.index('...'); ...` (`281` occurrences!) caused repeated `ValueError: substring not found`, indentation mismatches, `ruff` import-order failures, and **silent accidental chaining** — e.g., in `fb9e1ee` Calls `#118, #161, #178, #186, #264, #276, #282, #299`, the agent chained a 3-line Markdown `open(p,'w').write(s)` directly into a **`180–340 s` full-repo `pytest`** in the same `Bash` call!
- In `9dae286.md`, tight ratchet ceilings (`screener_search.py: loc grew from 384 to 385`, `landscape.py::_lamps: new cognitive complexity 23 exceeds 15`, `carry_intent_accounting.py::_indexed_intents: 20 exceeds 15`) were only discovered **after** writing the whole module and running slow gates, because there is no `< 0.3 s` single-file complexity/LOC pre-check helper!

---

### B10. Cold Real-Data Startup on Workstation (`~7–10 s` Cold `/api/proof/catalog`) & Unresolved Dual-Pipeline Architecture (`F2`)

**Evidence in `fb9e1ee.md` Calls `#110, #172, #207, #231` & `agent-performance-plan.md:134–137`:**
1. **Workstation cold `/api/proof/catalog` still takes `5.5–8.0 s` (`Call #231: 8,021.8 ms`, `Call #110: 6,730.2 ms`, `Call #207: 6,437 ms`):**
   - Because `CalendarData._load()` parses hundreds of raw JSON files from `data/spread-history-cache/*.json` (`~3.4 s`) and eagerly builds the 1h calendar catalog (`~3.0 s`) in memory on every server start — and because `remote sync` restarts `launchd` on every push — **every workstation restart and every `remote bench` candidate server pays a 6.5–8.0 s cold-catalog tax**!
2. **Architectural root of multi-file churn (`F2` dual-pipeline):**
   - When fixing the `-0.01 RUB` fixed-size rounding discrepancy in `fb9e1ee.md` (`Turn 35`, `f104a2c`), the agent had to inspect and patch **both** `mean_reversion_payloads.py` (legacy built-in path) **and** `fixed_plugin_payload.py` (plugin path), plus tests for both, because `F2` (dual built-in vs plugin execution paths) still duplicates accounting/payload assembly across `packages/proof` and `packages/web`.

---

## 3. Strategic Architecture of Generation-2 Optimizations

To achieve a **step-function reduction (3x–6x)** in end-to-end wall-clock task completion time — not merely 10% micro-tweaks — we must attack **all four multipliers** of total task duration:

$$\text{Total Task Wall Time} = N_{\text{turns}} \times \Big( T_{\text{human\_wait}} + N_{\text{slices}} \times \big( T_{\text{edit/debug}} + T_{\text{lint/type}} + T_{\text{pytest}} + T_{\text{handoff}} \big) \Big)$$

| Lever | Current Observed Reality (Logs A, B, C) | Target After Generation-2 Plan | Structural Change Required |
|---|---:|---:|---|
| **1. Turn & Slice Cadence ($N_{\text{turns}}, N_{\text{slices}}$)** | `12–24` micro-commits/session; stops for `"продолжи"` every 2 min (`10–15` human waits) | **`1–3` cohesive phase commits/session; `0` unnecessary stops** | Autonomous multi-slice loop within turn budget (`<= 120` calls); batch inner extractions; push + Mac sync **once per completed logical deliverable** |
| **2. Inner & Pre-Commit Verification ($T_{\text{pytest}}$)** | Full 605-test suite (`85–338 s`) run on **every** edit/doc slice (`39` full runs = `96.5 min`!) | **`0 s` on docs; `2–12 s` DAG-impacted tests per slice; `< 25 s` fast workspace suite (`-m "not slow_equiv"`)** | `scripts/verify_slice.py` (diff-aware DAG test selector) + `@pytest.mark.slow_equiv` split + session-cached fixture templates |
| **3. Static & Code-Health Gate ($T_{\text{lint/type}}$)** | `check_health.sh` (`15–38 s`) + `code_health_gate.py` (`15 s`) run **145 times** (`39.3 min`) | **`< 1.2 s` incremental gate (`check_health.sh --changed`); `< 8 s` full gate** | Diff-scoped `ruff` + `complexipy` + `LOC/token` check (`scripts/quick_check.py`), quiet-on-pass output, CPU-bounded parallelism, auto-classified `_LAYER_SPECS`, PEP 420 stub/typing fix |
| **4. Environment, Browser & Workstation I/O ($T_{\text{handoff}}$)** | `189 s` browser smokes; `22–52 s` `remote bench` (`copytree` + `uv sync`); `6–8 s` cold Mac restart on every push | **`< 35 s` browser smokes; `< 3 s` `remote bench` setup (APFS `cp -cR` + shared `.venv`); `< 1.5 s` smart `remote sync`** | Shared-Chromium/server in-process smoke runner + APFS CoW worktrees + no-restart doc/noop sync + persisted disk catalog cache on Mac |

---

## 4. Prioritized Work Packages (`AP2-P0` through `AP2-P7`)

Ordered by **wall-clock minutes saved per session** relative to implementation complexity. All items preserve Stage-9 invariants (`G1` bit-for-bit `run_id` parity, `G12` zero SaaS / zero committed secrets, zero production dependency bloat).

| ID | Work Package | Scope / Complexity | Est. Build Effort | Expected Time Saved | Primary Targets |
|---|---|---|---:|---|---|
| **`AP2-P0`** | **Diff-Aware Verification Engine (`scripts/verify_slice.py`) & Fast/Slow Pytest Tier Split** | Medium (Repo infra + pytest markers) | ~1.5 h | **Saves 45–70 min / session** (cuts `85–338 s` per-slice runs to `0–15 s`, and full fast suite to `< 25 s`) | `scripts/verify_slice.py`, `pyproject.toml`, `packages/proof/tests/test_perf_equivalence.py`, `tests/conftest.py` |
| **`AP2-P1`** | **Sub-Second Incremental Health Gate (`--changed`), Quiet Output & Zero-Touch Layer Classification** | Small–Medium (Scripts + DAG test) | ~1.0 h | **Saves 15–25 min / session** (`32 s -> < 1.2 s` inner checks; eliminates `_LAYER_SPECS` failures) | `scripts/check_health.sh`, `scripts/code_health_gate.py`, `packages/core/tests/test_architecture_dag.py` |
| **`AP2-P2`** | **Autonomous Multi-Slice Execution Protocol (Eliminate `"продолжи"` Ping-Pong & Doc-Only Test Runs)** | Small (Workflow & `AGENTS.md` contract) | ~30 min | **Eliminates 10–20 human-wait pauses / session** + cuts redundant pushes/Mac restarts by **70–85%** | `AGENTS.md`, `docs/development/agent-performance-plan.md` |
| **`AP2-P3`** | **One-Shot Sandbox Bootstrap & Credential/Bridge Env Loader (`scripts/agent_bootstrap.sh`)** | Small (Dev/sandbox tooling) | ~45 min | **Saves 5–12 min / session** + eliminates `PATH`, `uv`, `git` auth, file-mode reset, and `arena-promt` regex errors | `scripts/agent_bootstrap.sh`, `AGENTS.md` |
| **`AP2-P4`** | **In-Process Shared-Browser Smoke Runner & Fast Smoke Trial Mode (`189 s -> < 35 s`)** | Medium (`packages/web` tests + runner) | ~1.5 h | **Saves 2.5–3 min per UI verification** (achieves `< 35 s` on 2 vCPUs, closing open `P2` gate) | `scripts/run_browser_smokes.py`, `packages/web/tests/browser/*`, `packages/proof/.../tuning.py` |
| **`AP2-P5`** | **Zero-Copy APFS `remote bench` Worktrees, Smart Conditional `remote sync`, & Persistent Catalog Cache** | Medium–Large (`core` remote + `strategies`/`proof` catalog cache) | ~2.5 h | **Saves 8–15 min on workstation tasks** + cuts Mac cold `/api/proof/catalog` from **`~8 s -> < 0.4 s`** | `packages/core/.../workstation_{bench,remote}.py`, `packages/strategies/.../calendar_spreads.py` |
| **`AP2-P6`** | **PEP 420 Namespace Type-Resolution Fix & Incremental `basedpyright` Architecture** | Medium–Large (Build/typing config) | ~2.0 h | **Unblocks `P7.2` strict typing** (eliminates `327` false missing-import errors & `10 s` full-repo typecheck tax) | `pyproject.toml`, `scripts/check_types.py`, `packages/core/src/.../json_types.py` |
| **`AP2-P7`** | **AST-Safe Refactoring CLI (`scripts/agent_ inspect/extract`) & Built-In Pipeline Unification (`F2`)** | Large (Architectural) | ~3–5 h | **Cuts `Bash` context bloat by ~60%** (`973 KB -> < 350 KB`) and halves dual-path proof edits | `scripts/agent_tools.py`, `packages/proof`, `packages/web` (`F2` in `phase7-backlog.md`) |

---

## 5. Detailed Technical Specifications per Work Package

### `AP2-P0` — Diff-Aware Verification Engine (`scripts/verify_slice.py`) & Fast/Slow Pytest Split

**Problem solved:** Agents currently have only two choices: manually guess a `test_*.py` path or run `uv run pytest -q -n auto --dist loadfile` (`85–190 s` with xdist, `265–338 s` sequential without `-n`). In `fb9e1ee.md` and `9dae286.md`, **`5,593 s` (`93.2 minutes`)** went into full-workspace `pytest` runs — including after pure Markdown edits and isolated single-package refactors.

#### Architectural & code changes:

1. **Create `scripts/verify_slice.py` — Single Authoritative Command for Agent Verification:**
   - Computes `git diff --name-only HEAD` (plus untracked files, or `--base <rev>` for pre-push checks across a batch of local commits).
   - **Rule 1 — Docs / Comments / Markdown fast-path (`< 0.8 s`):**
     - If changed files are strictly `*.md`, `docs/**` (excluding `docs/inputs/manifest.json`), or `.gitignore`: runs **only** markdown link validation + `secret-scan --tracked-only`, skipping `ruff`, `basedpyright`, and `pytest` completely! *(In `fb9e1ee.md`, this single rule would have saved **`> 18 minutes`** across Calls `#106, #118, #161, #178, #186, #264, #276, #282, #299`!)*
   - **Rule 2 — Layer-DAG Impacted Package Selection (`2–25 s`):**
     - Uses the exact 6-layer dependency graph (`core -> {data, accounting} -> strategies -> proof -> web`):
       - Touching `packages/web/**` -> runs `packages/web/tests` only (`~4 s`).
       - Touching `packages/proof/**` (internal module split with unchanged public signatures) -> runs `packages/proof/tests -m "not slow_equiv"` (`~18–22 s`) + `packages/web/tests/test_http_routes.py` (`~3 s`), skipping `core`, `data`, `accounting`, `strategies`!
       - Touching `packages/strategies/<plugin>/**` -> runs `packages/strategies/tests` + plugin integration tests in `packages/proof/tests/test_*plugin*.py`.
       - Touching `packages/data/**` or `packages/accounting/**` -> runs that sibling package's tests + downstream contract tests (never running the other sibling!).
   - **Rule 3 — Pre-Push Mode (`scripts/verify_slice.py --pre-push`):**
     - Compares `origin/main..HEAD`, runs incremental `check_health.sh --base origin/main`, runs all transitive downstream package tests, and runs `test_perf_equivalence.py` **only** when files in `core/_fastcore*`, `accounting`, `strategies`, or `proof` execution/search paths changed (never when only `web`, `remote_cli`, `docs`, or CLI help changed!).
   - **Hard ban on accidental sequential full-suite runs:** `verify_slice.py` always passes `-n auto --dist loadfile -q --tb=short` so an agent never forgets `-n auto` and burns `338 s` on a 1-core sequential run.

2. **Separate Heavy Stress/Equivalence Replays (`@pytest.mark.slow_equiv`) & Deduplicate Fixture I/O:**
   - Register marker `slow_equiv` in `pyproject.toml` (`[tool.pytest.ini_options].markers`).
   - Mark the heavy multi-cohort double-replay methods in `packages/proof/tests/test_perf_equivalence.py` (`test_fastcore_c_matches_python_fallback_bit_for_bit`, `test_spawn_pool_matches_sequential_bit_for_bit`, `test_1h_pair_screen_cohort_search_plateau_windows_and_single_run`, `test_1h_calendar_proxy_and_native_cohort_and_mixed_universe`) while keeping 1 fast canary equivalence test in default runs, **or** split `test_perf_equivalence.py` (`1,285` LOC — also currently the #2 largest baseline file in `config/code-health-baseline.json`!) into:
     - `test_perf_canary.py` (`~150` LOC, `< 3 s` — runs on every `proof` test run);
     - `test_perf_equivalence_1d.py`, `test_perf_equivalence_1h.py`, `test_perf_pool_and_c_parity.py` (`< 400` LOC each — run in `--pre-push` when proof/accounting/strategies/C change, and always in CI `perf-equivalence`!).
     - *Bonus:* Splitting `test_perf_equivalence.py` into 3 files also allows `pytest -n auto --dist loadfile` to run them **in parallel across 3–4 workers** instead of pinning 1 worker for 40+ seconds!
   - **Session-cached fixture template (`tests/fixture_cache.py`):**
     - Build `fixture()` and `_build_h1_workspace()` **once per xdist worker process** in a read-only template directory and clone via `shutil.copytree(..., copy_function=os.link)` (instant hardlink clone on Linux tmpfs/ext4 & macOS APFS when tests only read inputs, or fast `shutil.copy2` for small JSONs), cutting fixture setup overhead across the 39 `proof` test files by **40–60%**.
   - **Eliminate 3x subprocess + full-repo copy in `test_architecture_dag_dropin.py`:**
     - Refactor `ArchitectureDagTests` / `_discover_modules(repo_root=..., extra_roots=...)` to accept a `repo_root` parameter in-process, or symlink unchanged packages (`core`, `data`, `accounting`, `proof`, `web`) in `tmp_path` and copy only `packages/strategies/src` (`< 5 ms`), dropping `packages/core/tests` runtime on 2 vCPUs from **`45.3 s -> < 3.5 s`**.

**Acceptance Gate (`AP2-P0`):**
- `verify_slice.py` on a Markdown-only change finishes in **`< 1.0 s`**.
- `verify_slice.py` on a `packages/web` or `remote_cli` change finishes in **`< 8.0 s`**.
- `verify_slice.py` on an internal `packages/proof` module split finishes in **`< 25.0 s`** (down from `100–220 s`).
- Splitting `test_perf_equivalence.py` removes a 1,206-LOC baseline entry and cuts parallel `perf-equivalence` wall time by **2–3x**.

---

### `AP2-P1` — Sub-Second Incremental Health Gate (`--changed`), Quiet Output & Auto-Classified `_LAYER_SPECS`

**Problem solved:** Agents invoked `check_health.sh`, `code_health_gate.py`, and `basedpyright` **145 times (`2,360 s` = 39.3 minutes)** across the 3 logs, waiting `15–38 s` (up to `104 s` under CPU contention) and flooding context with `223 KB` of passing check banners just to verify 1–3 edited files. In addition, adding any helper file in `core`/`data`/`accounting` broke hardcoded `_LAYER_SPECS`.

#### Architectural & code changes:

1. **Add `--changed` (inner-loop, `< 1.5 s`) and `--quiet` (default concise summary) to `scripts/check_health.sh` & `scripts/code_health_gate.py`:**
   - **`./scripts/check_health.sh --changed`:**
     - Resolves touched `.py`, `.c`, `.js`, `.css`, `.html` files vs `HEAD` (or `--base <rev>`).
     - Runs `ruff check <touched_py>` + `ruff format --check <touched_py>` (`~0.12 s`).
     - Runs `code_health_gate.py --files <touched>` (`~0.25 s` — checks LOC/token budgets, broad-exception markers, and `complexipy <touched_py>` **only on modified files**, instead of running `complexipy` + `lizard` across all 350+ repo files!).
     - Runs `basedpyright <touched_py>` (`~1.2–1.8 s` instead of `10–15 s` full workspace).
     - Runs `check_js_modules.py` (`~0.15 s`) **only** if `packages/web/.../static/**` changed.
     - Runs `architecture_gate.py --fast` (AST DAG check only, skipping full-repo `secret-scan` until commit/pre-push) **only** if `import` lines changed or files were added/moved.
   - **Cap concurrency in full `check_health.sh` to available vCPUs (`nproc`) and suppress passing log spam:**
     - Only print `cat "$logs/$name.log"` when `wait` returns non-zero (or `--verbose` is passed). On pass, print a single 1-line summary: `check_health: all 7 checks passed in 11s` (**saves ~200 KB of context tokens per session!**).
     - On `<= 2` vCPU sandboxes, schedule CPU-heavy `basedpyright` and `complexipy` sequentially or with bounded job slots (`MAX_JOBS=$(nproc)`) so 7 Python/Node/Rust processes don't thrash 2 cores for `38 s`.

2. **Filesystem Auto-Classification for ALL 6 Workspace Packages in `test_architecture_dag.py` (Zero Manual `_LAYER_SPECS` Edits):**
   - Currently `test_architecture_dag.py:59–62` auto-discovers `proof_runtime.*` and `web.*` from disk, and lines `98–105` auto-discover `strategies.*`, **while `core.*`, `data.*`, and `accounting.*` still require manual string edits in `_LAYER_SPECS`!**
   - **Fix:** Auto-classify **every** module under `packages/<pkg>/src/moex_spread_lab/<domain>/**/*.py` directly from its package directory:
     - `packages/core/src/...` -> `"core"` (except `cli.py` -> `"cli"`)
     - `packages/data/src/...` -> `"data"` (except `cli.py` -> `"cli"`)
     - `packages/accounting/src/...` -> `"accounting"` (except `cli.py` -> `"cli"`)
     - `packages/strategies/src/...` -> `"strategies"` (except `cli.py` -> `"cli"`)
     - `packages/proof/src/...` -> `"proof"` (except `cli.py` -> `"cli"`)
     - `packages/web/src/...` -> `"web"` (except `cli.py` -> `"cli"`)
   - Keep explicit list **only** for the frozen root compatibility shims in `src/moex_spread_lab/*.py` (which should never grow anyway!).
   - **Impact:** Extracting or adding a helper module anywhere in `packages/{core,data,accounting,strategies,proof,web}` will **never again fail `Unclassified modules`** (`fb9e1ee` Calls `#68, #295` = `150 s` wasted) or cause cross-branch merge conflicts in `test_architecture_dag.py`!

**Acceptance Gate (`AP2-P1`):**
- `./scripts/check_health.sh --changed` on a 2-file Python edit completes in **`< 2.0 s`** on 2 vCPUs and prints `<= 3` lines on success (`<= 25` lines on failure, showing only failing checks).
- Creating `packages/core/src/moex_spread_lab/core/_probe_helper.py` passes `test_architecture_dag.py` with **zero edits** to `test_architecture_dag.py`.

---

### `AP2-P2` — Autonomous Multi-Slice Execution Protocol (Eliminate `"продолжи"` Ping-Pong & Batch Workstation Syncs)

**Problem solved:** Look at the stark contrast between `b687ae2.md` (`1` assistant turn, `127` calls, `0` user nudges, ~13 min wall-clock) and `9dae286.md` (`13` assistant turns, `11` manual `"продолжи"` messages, `12` full-suite runs, `12` Mac restarts for a partial `P7.2`). The agent in `9dae286.md` treated every ~80-line helper extraction as a mandatory **human stop boundary** because `AGENTS.md` / `P4` conflated **atomic git commits** with **stopping the assistant turn**.

#### Operational & workflow changes (`AGENTS.md` + Prompt Convention):

1. **Separate "Atomic Commit Cadence" from "Assistant Turn Stop Condition":**
   - **New rule in `AGENTS.md`:**
     - **Within one user turn, continue autonomously across internal sub-slices** until either:
       1. The requested work package (e.g., all of `P7.2` or `P7.4`) is **100% complete**;
       2. A genuine product/accounting decision requires user input (e.g., the `-0.01 RUB` rounding policy choice in `fb9e1ee` Turn 34); or
       3. The turn approaches **75% of the tool-call budget (~140–160 calls)** — only then checkpoint, push, sync, and ask to continue.
     - **Never stop after 9–15 tool calls (`2 min`) just to ask `"продолжи"`** when no blocker exists and budget remains!
2. **Two-Tier Commit vs Push/Workstation-Sync Cadence:**
   - **Inner sub-slices (`Slice 1..N-1` of a multi-file refactor):**
     - Verify with `check_health.sh --changed` (`< 2 s`) + `verify_slice.py` (`< 20 s`) -> create local atomic `git commit` (preserving clean rollback points and crisp `git blame`). **Do NOT run full 6-package `pytest`, `git push`, or `remote sync` after every 80-line helper extraction!**
   - **Phase / Turn Completion (`Slice N`, once at end of turn):**
     - Run `code_health_gate.py --update-baseline` -> `./scripts/check_health.sh` -> `verify_slice.py --pre-push` -> `git push origin main` -> single `moex-spread-lab remote sync && remote status`.
   - **Measured savings on a 12-commit refactor like `9dae286.md`:**
     - Replaces `12 x 83 s` full pytests (`996 s`) + `12 x 16 s` full health gates (`192 s`) + `12 x 6 s` Mac restarts (`72 s`) + `11` human round-trip waits with **`11 x 12 s` focused checks (`132 s`) + `1 x 95 s` final gate = `227 s` (`3.8 min` instead of `30.3 min` shell + human waits — an 8x speedup!)**.

---

### `AP2-P3` — One-Shot Cloud Sandbox Bootstrap & Unified Runtime Env (`scripts/agent_bootstrap.sh`)

**Problem solved:** Every cloud session (`fb9e1ee`, `b687ae2`, `9dae286`) wasted `5–15` tool calls and several failures on:
- Missing `uv` in `PATH` (`export PATH="$HOME/.local/bin:$PATH"` repeated 40+ times);
- Git credential helper setup (`exit 128` on push, or unsafe PAT embedded in `.git/config` remote URL);
- Re-parsing `arena-promt.md` / `arena-promt-2.md` with fragile inline Python regexes on 39 separate calls;
- Mid-session container hibernation resetting file modes (`100755 -> 100644` in `fb9e1ee` Call `#305`) and losing `uv` / git config;
- Slow cold `_fastcore.c` compilation during the first test run instead of precompiling once during bootstrap.

#### Implementation specification:

1. **Add `scripts/agent_bootstrap.sh` (idempotent, `< 8 s` warm / `< 15 s` cold, sourceable via `. scripts/agent_bootstrap.sh`):**
   - Ensures `uv` is installed and writes `~/.profile` / `.envrc`-style shim or symlinks `uv` into a standard `PATH` directory (`/usr/local/bin` if writable or `~/.local/bin` exported cleanly).
   - Configures `git config core.fileMode false` (or restores `+x` on tracked `100755` scripts automatically if container reset stripped mode bits!) and sets `user.name` / `user.email` if unset.
   - Configures credential-store cleanly from `$GITHUB_TOKEN` / `$GH_TOKEN` / `$MSL_GIT_PAT` **without ever writing tokens into `.git/config` remote URLs**.
   - Auto-discovers any `/home/user/uploads/arena-promt*.md` or `/home/user/uploaded_files/arena-promt*.md` (picking the newest file so `arena-promt-2.md` overrides `arena-promt.md` automatically! — fixing the exact bug in `fb9e1ee` Calls `#18, #20`) and exports `ARENA_BRIDGE_URL` and `ARENA_BRIDGE_TOKEN` into an untracked, gitignored `.agent-env` (`0600` permissions, excluded by `.gitignore` and never scanned by `--tracked-only`).
   - Runs `uv sync --frozen` and precompiles `_fastcore` (`load_fastcore()`) once.
2. **Auto-load `.agent-env` in `remote_cli.py` (fallback when env vars are unset):**
   - If `ARENA_BRIDGE_URL` / `ARENA_BRIDGE_TOKEN` are not in `os.environ`, `remote_cli.py` checks untracked `<repo_root>/.agent-env` (strictly verifying `.agent-env` is git-untracked before reading).
   - **Result:** After `. scripts/agent_bootstrap.sh` on Call #1, **every subsequent `uv run moex-spread-lab remote status|sync|probe|bench` works with zero `export PATH=...` and zero inline regex `arena-promt.md` parsing!**

---

### `AP2-P4` — In-Process Shared-Browser Smoke Runner & Fast Smoke Trial Mode (`189.1 s -> < 30–35 s`)

**Problem solved:** `P2` in `agent-performance-plan.md` remained open (`[ ] Step 3`) because `scripts/run_browser_smokes.py` still took **`189.1 s`** on a 2-vCPU sandbox (and now has 12 suites). Each suite starts a separate Python interpreter, builds synthetic JSON fixtures from scratch, starts a new `ThreadingHTTPServer`, launches a cold Chromium instance, and runs **dozens of 80-trial-per-family Optuna searches** (`checkbox_tuning_smoke.py` alone runs 6+ full optimizations = `32.3 s`; `strategy_search_smoke.py` = `26.5 s`).

#### Architectural & code changes:

1. **In-Process Shared-Chromium Mode in `scripts/run_browser_smokes.py` (`--shared-browser`, default):**
   - Standardize every `packages/web/tests/browser/*_smoke.py` to expose `run_smoke(browser: Browser) -> None` (keeping `if __name__ == "__main__":` standalone compatibility via `_harness.py`!).
   - In `run_browser_smokes.py`: launch `sync_playwright()` and **`pw.chromium.launch()` ONCE** per worker, then run each suite inside its own isolated `BrowserContext` (`browser.new_context()`) + isolated `serve_fixture(...)` server!
   - **Savings:** Eliminates **11 of 12 cold Chromium startups** and **11 of 12 Python/PyArrow/DuckDB/Optuna interpreter imports** (`~50–55 s` saved immediately!).
2. **Template-Cached Synthetic Fixtures in `_harness.py`:**
   - Many smoke scripts (`automatic_size_smoke`, `checkbox_tuning_smoke`, `global_capital_smoke`, `group_lamps_smoke`, `period_settings_smoke`, `screen_settings_smoke`, `seed_settings_smoke`, `strategy_search_smoke`, `window_table_smoke`) call the exact same `period_settings_smoke.fixture(root)` builder!
   - Cache the built directory by `builder.__qualname__` in a temporary template folder inside `_harness.py` and clone it per suite (`~1 ms` vs `~0.8–1.5 s` repeated JSON generation).
3. **Bounded Optuna Trial Budget for UI DOM Smokes (`MOEX_UI_SMOKE_TRIALS=12`):**
   - UI browser smokes test **DOM wiring, tri-state checkboxes, modal prompts, streaming progress bars, lamp rendering, and URL/state sync** — whereas numerical 80-trial search optimality is already exhaustively tested in `packages/proof/tests/test_proof_search.py`, `test_strategy_search.py`, and `test_perf_equivalence.py`.
   - Support an explicit test-only env override `MOEX_PROOF_SMOKE_TRIALS` (honored **only** when `MOEX_OFFLINE_ONLY=1`, never in normal research runs or when a smoke explicitly unsets it to test exact trial counts like `seed_settings_smoke.py` if needed) reducing per-click Optuna trials in browser smokes from `80/family` (`240–320` evaluations per click!) to `10–12/family` (`30–36` evaluations).
   - **Savings:** Cuts `checkbox_tuning_smoke.py` (`32.3 s -> ~5 s`), `strategy_search_smoke.py` (`26.5 s -> ~5 s`), `global_capital_smoke.py` (`20.6 s -> ~4 s`), `group_lamps_smoke.py` (`20.2 s -> ~4 s`), bringing **all 12 browser smokes from `189.1 s` down to `~25–35 s` total on 2 vCPUs**!

**Acceptance Gate (`AP2-P4`):**
- `uv run --with playwright python scripts/run_browser_smokes.py` passes **12/12** suites on a 2-vCPU Linux sandbox in **`< 45 s`** (over 4x faster than `189.1 s`, comfortably beating the original `< 120 s` P2 target).

---

### `AP2-P5` — Zero-Copy APFS `remote bench`, Smart Conditional `remote sync`, & Persistent Workstation Catalog Cache

**Problem solved:** Three compounding workstation bottlenecks slowed `fb9e1ee.md` (`758 s` across 105 bridge calls) and degraded local UX after every sync:
1. `workstation_bench.py:_copy_data()` physically copies hundreds of MB of `data/` and `data-lake/` byte-by-byte AND runs `uv sync --frozen` on every candidate benchmark (`22–52 s`);
2. `workstation_remote.py:sync()` runs `uv sync --frozen`, recompiles/checks `_fastcore`, and kills/restarts `launchd` even on docs/test-only commits — and returns *before* waiting for `http://127.0.0.1:8765/api/dev/health` to come back up (forcing a second `remote status` call and causing `HTTP 000` race conditions);
3. Cold `/api/proof/catalog` on real workstation data still takes **`6.5–8.0 s`** (`~3.4 s` parsing hundreds of JSON files in `data/spread-history-cache/` + `~3.0 s` building 1h calendar windows) on every server restart and every `candidate_server` launch!

#### Architectural & code changes:

1. **APFS Copy-on-Write (`cp -cR`) + Shared `.venv` Fast-Path in `workstation_bench.py` (`22–52 s -> 2–4 s` setup):**
   - In `_copy_data(root, candidate)`: on macOS (`sys.platform == "darwin"`), try `subprocess.run(["cp", "-cR", str(src), str(dst)])` first (falling back to `shutil.copytree` on cross-device/non-APFS filesystems). `cp -cR` uses kernel APFS `clonefile(2)`: **100% copy-on-write isolation** (mutations in candidate never touch `root/data`!), zero symlinks, and **500 MB clones in `< 40 ms` instead of `8–15 s`**!
   - Skip `uv sync --frozen` inside `candidate_worktree` unless `patch` (or `revision` diff vs `root` `HEAD`) modifies `pyproject.toml`, `uv.lock`, `packages/*/pyproject.toml`, or `_fastcore.c`! Instead, run `candidate_server` using `root/.venv/bin/python3` with `PYTHONPATH` prepending `candidate/packages/*/src` and `candidate/src` (and `MOEX_NO_EDITABLE_LEAK=1` verified via `module.__file__` assertion in `/api/dev/health`).

2. **Smart Conditional `remote sync` with Built-In Readiness Wait (`workstation_remote.py`):**
   - Inspect `changed = git diff --name-only old..origin/main`:
     - **If `old == new`:** return immediately (`{"noop": true, ...}` in `< 0.5 s`, zero `uv sync`, zero server restart!).
     - **Run `uv sync --frozen` ONLY if** `uv.lock` or any `*pyproject.toml` changed (or `.venv` is missing).
     - **Restart `launchd` ONLY if** runtime files (`packages/*/src/**` or `src/**` or `config/**`) changed! If a push only touched `docs/**`, `AGENTS.md`, `.github/**`, or `packages/*/tests/**`, fast-forward the git checkout **without restarting `launchd`**, keeping the user's workstation caches and worker pool 100% warm!
     - **When `launchd` IS restarted:** poll `http://127.0.0.1:8765/api/dev/health` locally on the Mac (`50 ms` intervals up to `10 s`) inside `sync()` itself and return the verified post-restart `status` payload in **one** bridge round-trip (eliminating the mandatory second `remote status` CLI call and `HTTP 000` race failures!).

3. **Persistent Fingerprint-Keyed Disk Cache for `CalendarData` / `ProofService.catalog()` (`8.0 s -> < 0.4 s` Cold Boot):**
   - Why does cold `/api/proof/catalog` take `6.5–8.0 s` on Mac? Because `CalendarData._load()` opens and `json.loads()` hundreds of immutable historical cache files (`data/spread-history-cache/*.json`) and recomputes 1d + 1h window summaries from raw rows on every process start.
   - **Solution:** Store a compact derived binary/JSON/Parquet snapshot under gitignored `data/.cache/catalog-v1-<fingerprint>.zst` (or pickle-free msgpack/Parquet/canonical JSON compressed with already-installed `zstandard`), where `<fingerprint>` is the SHA-256 over `(code_schema_version, sorted (filename, st_mtime_ns, st_size) of data/spread-history-cache/, data/w1/, data/benchmark/, h1-seal.enabled.json)`.
   - **Guarantee:** Any added, deleted, or modified data file changes `(filename, mtime_ns, size)` -> instant cache miss & rebuild (100% bit-for-bit identical to uncached `_load()`, verified by an equivalence test like `test_file_caches_invalidate_immediately_on_in_place_same_size_edits`). On unchanged data (99.9% of server restarts and candidate benchmarks!), cold `/api/proof/catalog` drops from **`~8.0 s -> ~0.25–0.40 s`**!

**Acceptance Gate (`AP2-P5`):**
- `remote sync` on a docs/test-only commit completes in **`< 1.5 s`** without restarting `launchd`.
- `remote sync` on a runtime commit returns verified `http_status: 200` and matching `dev_health.git_head` in a **single command**.
- Cold workstation `/api/proof/catalog` after `launchctl kickstart -k` completes in **`< 0.8 s`** (down from `6.4–8.0 s`) with byte-for-bit identical SHA-256 payload.

---

### `AP2-P6` — Resolve PEP 420 Namespace Type-Checking Barrier & Speed Up `basedpyright` (`P7.2` Unblocker)

**Problem solved:**
1. `basedpyright` is the single slowest step inside `check_health.sh` (`~10–15 s` on 4 cores, `~25–30 s` on 2 cores) because it re-analyzes all 7 workspace roots every time.
2. More importantly, `9dae286.md` proved that `P7.2`'s goal (*"enable `[tool.basedpyright].strict` across `data`, `accounting`, `strategies`, `proof`"*) is currently **blocked by 6,352 errors**, including **327 `reportMissingImports` false positives** on PEP 420 namespace packages when `strict = [...]` overrides `reportMissingImports = "none"`.
3. In `packages/core` (already `strict`), untyped `json.load()` / `dict[str, Any]` boundaries caused 11 failed edit-check cycles in `fb9e1ee.md`.

#### Architectural & code changes:

1. **Fix PEP 420 Namespace Resolution Properly via `executionEnvironments` (Not `strict = [...]` Array):**
   - In `basedpyright` / Pyright configuration, listing paths in `strict = ["packages/proof/src"]` applies Pyright's built-in `"strict"` diagnostic preset *on top of* root settings, **re-enabling `reportMissingImports = "error"`**!
   - Instead, configure `[tool.basedpyright]` using `executionEnvironments` (or explicit `typeCheckingMode = "strict"` per directory with `reportMissingImports = "none"` preserved via a unified virtual namespace link farm `.venv/lib/.../moex_spread_lab` or editable `.pth` unification):
     - **Root cause of the 327 `reportMissingImports` false positives:** `setuptools` editable installs create 7 separate `_editable_impl` finder hooks in `.venv/lib/python3.12/site-packages`, which static analyzers (`basedpyright`) cannot execute, while `extraPaths` with 7 directories containing `moex_spread_lab/` causes Pyright's single-package resolver to match the *first* `moex_spread_lab` folder (`packages/core/src/moex_spread_lab`) and miss submodules in `packages/data/src/moex_spread_lab` under strict mode!
     - **Clean deterministic fix (`scripts/sync_typecheck_namespace.py`, `< 30 ms`):** Generate a gitignored `.typecheck_typings/moex_spread_lab/` symlink farm pointing each domain subpackage (`core`, `data`, `accounting`, `strategies`, `proof_runtime`, `web`, plus root `cli.py`) into **one unified package directory**, and set `extraPaths = [".typecheck_typings", "scripts"]`!
     - **Result:** All **327 false `reportMissingImports` vanish permanently**, `basedpyright` resolves cross-package types with **100% fidelity** (catching real missing imports too, so `reportMissingImports = "error"` can actually be turned **ON**!), and Pyright's module resolution speed improves!
2. **Add Typed JSON Boundary Helpers in `msl-core` (`packages/core/src/moex_spread_lab/core/json_types.py`):**
   - Provide `JsonValue`, `JsonObject = dict[str, Any]`, `loads_object(raw: str | bytes) -> dict[str, Any]`, `load_object(path: Path) -> dict[str, Any]`, and `as_str_dict(val: object) -> dict[str, Any]` in `msl-core`.
   - Eliminates the #1 cause of `basedpyright` strict-mode errors (`reportUnknownVariableType` / `reportUnknownMemberType` / `reportUnknownArgumentType` after `json.load` / `json.loads`), cutting typing churn during future package migrations (`P7.2`, `P7.4`) by >70%.
3. **Incremental Module-by-Module Strict Onboarding (`executionEnvironments`):**
   - Enable strict mode **one subpackage / leaf module group at a time** (starting with already-clean leaf modules extracted in `9dae286.md`: `axis.py`, `landscape.py`, `evaluation.py`, `request_validation.py`, `tuning.py`, `worker_weight.py`, `worker_cohort.py`, `cohort_scores.py`, `cohort_models.py`), rather than flipping 3,675 errors at once across legacy dict-heavy modules.

---

### `AP2-P7` — Compact Agent Inspection/Refactoring CLI & Unifying Dual Proof Pipelines (`F2`)

**Problem solved:**
1. **`973 KB` (`47%`) of all tool I/O** across the 3 logs came from `Bash` `sed -n` / `cat` / `grep` dumps, and **`281` file edits** used error-prone inline `python - <<'PY' s.index(...)` scripts — frequently tripping LOC/complexity ratchets *after* writing files (`9dae286.md` Calls `#25, #135, #140`).
2. **Architectural root cause of duplicated edits (`F2`):** Built-in strategies and drop-in plugins still traverse two parallel execution/payload pipelines (`mean_reversion_payloads.py` vs `fixed_plugin_payload.py`, hardcoded `_BUILTIN_STRATEGY_IDS` across `proof` and `web`), doubling the files an agent must read, edit, and test for every accounting or UI change (as directly seen in `fb9e1ee.md` Turn 35).

#### Architectural & code changes:

1. **Add `scripts/agent_inspect.py` (Zero-Bloat Outline, Complexity & Blast-Radius Preview in `< 0.2 s`):**
   - In Arena / cloud sandboxes where workstation `repowise` MCP is not mounted and structured `Edit` may be absent, give agents a lightweight CLI specifically designed to minimize token footprint:
     - `uv run --no-sync python scripts/agent_inspect.py outline <file.py>` — prints imports, class/function signatures, line ranges, current LOC/tokens, and **per-function cognitive complexity** in `~25` lines instead of dumping `400` lines via `sed -n`!
     - `uv run --no-sync python scripts/agent_inspect.py callers <symbol>` — AST/token-exact cross-repo caller & re-export finder (distinguishes production callers vs test callers vs compatibility facades in `< 0.3 s`).
     - `uv run --no-sync python scripts/agent_inspect.py check <file1> [file2 ...]` — runs `ruff check` + `ruff format --check` + `LOC/token` check + `complexipy` + `basedpyright` on **just those files** in **`< 1.5 s`**, printing exact per-function complexity scores *before* running full gates.
2. **Execute `F2` (`P7.3` — Unify the 4 Built-In Strategies onto the Validated Intent-Adapter Pipeline):**
   - Prioritized architectural cleanup from [`phase7-backlog.md`](../phase7-backlog.md): migrate `calendar_zscore` + `calendar_ruonia` -> `calendar_price_intents.v1`, `pair_mean_reversion` -> `pair_price_intents.v1`, and `funding_carry` -> `carry_settlement_intents.v1` (one family per PR, guarded by bit-for-bit `run_id` parity).
   - **Impact on future agent task speed:** Deletes ~800–1,000 LOC of duplicate legacy adapter/translate/UI branching across `packages/proof` and `packages/web`, cutting the blast radius of future S4 / strategy / accounting tasks in half.

---

## 6. Projected End-to-End Impact on Future Agent Sessions

Applying `AP2-P0` through `AP2-P6` to the exact three workloads observed in the logs yields the following deterministic time reductions:

### 6.1 Replay Projection on Observed Workloads

| Session / Workload | Observed Shell Time | Observed Wall-Clock & Human Waits | Projected Shell Time (Post-`AP2`) | Projected Wall-Clock & Human Waits | Key Drivers of Speedup |
|---|---:|---:|---:|---:|---|
| **Session A (`fb9e1ee` — `P1`+`P2`+`P3`+rounding, 24 commits)** | **`130.3 min`** (`7,820 s`) | **~3h 50m active** (`20` turns, `14` nudges) | **`~22–28 min`** (`~1,500 s`) | **~35–45 min** (`3–4` turns) | Skip 9 doc-only full pytests (`-32 min`); DAG-scoped slice tests (`-35 min`); `--changed` health gate (`-22 min`); `<35 s` browser smokes (`-2.5 min`); APFS CoW `remote bench` (`-7 min`) |
| **Session B (`b687ae2` — `P7.1`, 2 commits)** | **`5.8 min`** (`349 s`) | **~13 min** (`1` turn, `0` nudges) | **`~2.2 min`** (`~130 s`) | **~6–7 min** (`1` turn) | Eliminate `104 s` contended `check_health` + split `perf_equivalence` + `agent_bootstrap.sh` + `agent_inspect.py` context reduction |
| **Session C (`9dae286` — `P7.2` 12-module proof split)** | **`30.3 min`** (`1,821 s`) | **~50 min** (`13` turns, **`11` `"продолжи"` waits**) | **`~4.5–6.0 min`** (`~310 s`) | **~10–14 min** (`1–2` autonomous turns, **`0–1` waits**) | Batch 12 inner extractions into 1–2 autonomous turns; run `<2 s` `--changed` gate + `<18 s` focused proof tests on slices `1..11`, and run full gate + Mac sync **once** at turn end; `.typecheck_typings` unblocks `strict` typing |
| **Total across 3 representative sessions** | **`166.5 min`** | **~4h 53m active + 25 human nudges** | **`~29–36 min`** (**~5x less shell wait**) | **~50–65 min** (**~4.5–5.5x faster wall-clock**, **>85% fewer human nudges**) | **Saves ~2.2 hours of compute wait and ~3.5+ hours of calendar latency per 3 tasks** |

---

## 7. Recommended Implementation Sequence (Execution Roadmap)

The plan is structured so the **highest-leverage, lowest-risk infrastructure (`Wave 1`) lands in ~2–3 hours** and immediately accelerates every subsequent refactoring and feature task (`P7.2` completion, `P7.3`/`F2`, `P7.4`, and Stage-9 `S4`):

```text
Wave 1 — Immediate Inner-Loop & Workflow Multipliers (~2.5 hours total, zero numerical risk):
  ├── Step 1.1 (AP2-P1): Auto-classify all 6 `packages/*` in `test_architecture_dag.py` + quiet/CPU-capped
  │                      `check_health.sh` + `./scripts/check_health.sh --changed` (< 2 s inner gate).
  ├── Step 1.2 (AP2-P0): Add `scripts/verify_slice.py` (doc fast-path, 6-layer DAG package test selector,
  │                      mandatory `-n auto --dist loadfile`), fix `test_architecture_dag_dropin.py`
  │                      (`45 s -> < 3 s`), and split `test_perf_equivalence.py` into parallelizable suites
  │                      + session-cached fixture templates.
  └── Step 1.3 (AP2-P2 + AP2-P3): Add `scripts/agent_bootstrap.sh` + `.agent-env` fallback in `remote_cli.py`,
                                  and update `AGENTS.md` with the Autonomous Multi-Slice Protocol
                                  (local commits per slice; full gate + push + `remote sync` once per turn/phase).

Wave 2 — Browser, Workstation & Type-System Accelerators (~4 hours total, parallelizable):
  ├── Step 2.1 (AP2-P4): Shared-Chromium in-process `scripts/run_browser_smokes.py` + cached smoke fixtures
  │                      + `MOEX_PROOF_SMOKE_TRIALS` (`189.1 s -> < 35 s` on 2 vCPUs; closes P2 gate).
  ├── Step 2.2 (AP2-P5): APFS `cp -cR` CoW + shared `.venv` in `workstation_bench.py`, conditional no-restart
  │                      `remote sync` with built-in HTTP 200 readiness wait, and fingerprint-keyed persistent
  │                      catalog cache (`8.0 s -> < 0.4 s` cold workstation boot).
  └── Step 2.3 (AP2-P6): `.typecheck_typings/moex_spread_lab` symlink unifier + `core/json_types.py` helpers
                         to eliminate all 327 PEP 420 false-positive missing imports and enable incremental
                         `basedpyright` strictness across `packages/*`.

Wave 3 — Structural Surface-Area Reduction (Ongoing alongside Phase 7):
  └── Step 3.1 (AP2-P7): Add `scripts/agent_inspect.py` (`outline`, `callers`, `check`) to slash `Bash`
                         `sed`/`cat` context bloat, then execute `F2` (`P7.3`) built-in intent-adapter
                         unification using the Wave 1–2 fast verification pipeline.
```

---

## 8. Updated Prompt & Operating Template for Multi-Step Tasks

Until `AGENTS.md` is updated in `Wave 1 (Step 1.3)`, use the following task-prompt formulation to avoid the `"продолжи"` ping-pong seen in `9dae286.md` and the redundant full-pytest runs seen in `fb9e1ee.md`:

```text
Выполни <пакет работ, например оставшуюся часть P7.2 или Wave 1 из docs/development/agent-performance-2/plan1.md> целиком.
Правила выполнения:
1. Работай автономно в рамках одного захода (не останавливайся после каждого мелкого подшага для «продолжи», пока задача не завершена, не возник содержательный вопрос по бизнес-логике или не израсходовано ~75% лимита вызовов).
2. На промежуточных срезах делай атомарные локальные коммиты после быстрых точечных проверок (ruff + basedpyright по изменённым файлам + целевые тесты затронутого пакета; для правок только docs/*.md pytest не запускай).
3. Полный check_health.sh, полный параллельный pytest (-n auto --dist loadfile), git push в origin/main и синхронизацию воркстейшна (remote sync) выполняй один раз в конце завершённого этапа.
```
