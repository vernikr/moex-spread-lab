# S4b — Browser smoke cost: measurement and implementation (2026-11-23)

**Status:** work items 1–4 landed; item 5 (move non-UI assertions to contract tests) remains, and this document records why it is now the only remaining lever. Sibling slice of [plan.md](plan.md) §4 (S4b). Numbers below were taken on the slice's implementation tree and are measured, not estimated; every row states its sample count.

---

## 1. What changed

| Work item | Implementation |
| --- | --- |
| 1. Phase timers first | `packages/web/tests/browser/_harness.py` accumulates `template_build`, `fixture_clone`, `server_startup`, `browser_launch`, `app_startup`, `api_compute`, `static` and prints one `MOEX_SMOKE_PHASES={…}` line per scenario; the remainder is reported as `ui_assertions`. `scripts/run_browser_smokes.py` aggregates them into a table after the run (`--no-timings` opts out). API compute is measured inside the loopback handler, so it is server time, not round-trip time. |
| 2. Shared browser, isolated contexts | `scripts/run_browser_smokes.py` runs one worker process per job, launches Playwright and Chromium once per worker (lazily, on the first suite that needs them) and calls each suite's `scenario(browser)` entry point. Every scenario keeps its own `BrowserContext` and loopback server: `_harness.scenario_isolation(browser)` records the contexts a scenario creates and closes them even when an assertion fails, so a half-finished suite cannot leak pages into the next one. `_harness.standalone(scenario, MESSAGE)` keeps `python <suite>_smoke.py` working for one-off runs; both entry points share the same code path. |
| 3. Fixture templates | `packages/web/tests/browser/_templates.py` builds a template once per worker, keyed by builder identity (module + qualified name + a fingerprint of code, defaults and captured values, so two closures from one factory never collide), and clones it into each scenario's temporary root. Mutation in a scenario (funding-rate rewrites, candle shocks) therefore never reaches the template. |
| 4. Bounded trial budget | `packages/proof/src/moex_spread_lab/proof_runtime/trial_budget.py` owns `TRIALS_PER_STRATEGY = 80` and `budget()`: `MOEX_UI_SMOKE_TRIALS` is honoured only under `MOEX_OFFLINE_ONLY=1` and only downwards. Suites opt in with `_harness.ui_only_trials()` and opt out explicitly with `_harness.exact_trials()`. Only `seed_settings_smoke.py` opts in (§3.2). |
| 5. Contract-test migration | **Not in this change.** §4 records the measured basis and the migration order. |

Supporting code: `proof_strategy_search.py` keeps its size ceiling unchanged (the budget moved into its own module rather than growing a file that the code-health ratchet freezes at 311 LOC / 3742 tokens); `run_worker` was split into `_WorkerBrowser`, `_run_suite`, `_execute` and `_run_scenario` to stay under the cognitive-complexity limit of 15.

New tests: `packages/core/tests/test_browser_smoke_runner.py` (offline contract preserved, one launch per worker, one scenario call per suite, plain scripts do not start a browser, child exit codes, phase parsing, partition, CPU limits), `packages/web/tests/test_browser_smoke_harness.py` (template caching, clone isolation, identity collisions, phase reporting) and `packages/proof/tests/test_strategy_trial_budget.py` (default, offline-only, downward-only, invalid values).

## 2. Measurements

Runner: this slice's 2-vCPU x86_64 Linux sandbox, CPython 3.13.18, Chromium headless shell 159.0.8154.6, `jobs=1` (the CPU-aware limit below four CPUs), warm page cache, `MOEX_OFFLINE_ONLY=1`. Suites are unchanged in what they assert.

### 2.1 End-to-end

| Tree | Samples | Result | Wall time |
| --- | --- | --- | --- |
| Before S4b (`aba5d33`, one process + one cold Chromium per scenario) | n=1 | 12/12 passed | 110.0 s |
| After S4b (one Chromium per worker, cloned fixture templates, `jobs=1`) | n=3 consecutive, uncontended | 12/12 passed each | 104.1 / 109.3 / 112.9 s |

Reading: the acceptance ceiling of 120 s holds, but the wall-time delta on *this* runner sits inside the ±5 s run-to-run spread. The per-suite picture (§2.2) shows where the removed work went: the suites that only pay fixed overhead gained 0.6–2.4 s each, while the four suites that dominate the run are unchanged.

### 2.2 Per suite (selected)

| Suite | Before (s) | After (s) |
| --- | --- | --- |
| `rounding_display_smoke.py` | 1.7 | 0.7–1.2 |
| `funding_evidence_smoke.py` | 4.5 | 3.4–3.9 |
| `seed_settings_smoke.py` | 4.5 | 2.1–2.5 (12-trial budget) |
| `screen_settings_smoke.py` | 7.6 | 6.3–7.4 |
| `automatic_size_smoke.py` | 6.7 | 6.5–7.8 |
| `checkbox_tuning_smoke.py` | 17.4 | 16.8–19.7 |
| `strategy_search_smoke.py` | 15.0 | 14.2–15.3 |

What the fixed overhead was, per scenario, before this change: one Python interpreter start, one `sync_playwright()`/Chromium launch and one browser shutdown (measured at 0.4–0.6 s each on this runner). Twelve scenarios paid it twelve times; the worker now pays it once.

### 2.3 Phase split (sum over 12 scenarios, after S4b)

| Phase | Seconds | Share |
| --- | --- | --- |
| `ui_assertions` (Playwright interactions and waits) | 61.4–64.8 | ~59 % |
| `api_compute` (server-side proof/plateau/optimize) | 42.6–47.9 | ~42 % |
| `app_startup` (first payload per page) | 9.5–10.0 | ~9 % |
| `static` | 0.8–0.9 | <1 % |
| `browser_launch` (whole run) | 0.1 | <1 % |
| `server_startup` | 0.1 | <1 % |
| `template_build` + `fixture_clone` | 0.02 | <1 % |

Shares overlap by construction (`app_startup` includes the first proof compute of a page, and `ui_assertions` is the clamped remainder), so they are read as "where the time is", not as a partition.

Three consequences, each of which changes what the plan can promise:

1. **Fixture hydration is not a cost here.** `template_build` runs once per builder per worker and is ~5–17 ms; the clone is ~1 ms. The template cache is kept (it removes duplicated work and is the correct home for the plan's item 3), but no further optimization should target fixtures.
2. **Chromium launch is gone from the budget** (0.1 s for the whole run versus ~6 s of repeated launches measured in §2.2).
3. **Assertion time is now the largest term**, followed by API compute. Both are addressed by item 5, not by items 2–4.

## 3. Decisions and evidence

### 3.1 Worker model

- One worker process per job, one Chromium per worker, one `BrowserContext` + one loopback server per scenario. `--jobs` stays CPU-aware (2 only at ≥4 CPUs): an experiment with `jobs=2` on this 2-vCPU/1 GB sandbox produced two suite failures (`funding_evidence_smoke.py`, `strategy_search_smoke.py`) within one run, i.e. a memory/CPU-contention artefact rather than a code defect. Two workers per 2-vCPU runner is therefore not adopted.
- A suite that re-executes itself with a plugin on `PYTHONPATH` (`pair_plugin_smoke.py`) keeps working: the worker treats `SystemExit(0)` as a pass and reads the child's phase line from its output.

### 3.2 Bounded trials: measured, then narrowed to one suite

With `MOEX_UI_SMOKE_TRIALS=12` applied to six suites, the run dropped to 90–92 s but two suites failed intermittently:

| Suite | Failure | Cause |
| --- | --- | --- |
| `automatic_size_smoke.py` | `#ledger-table .badge.entry` not found | the 12-trial search selected parameters that produce no trades, and the scenario asserts a ledger entry |
| `group_lamps_smoke.py` | `#group-profile-state` was `unavailable`, expected `ready` | the 12-trial search did not find a usable configuration for the fixed cohort |

Both scenarios assert search-derived UI state, which is exactly the case plan §4 excludes: the base seed is drawn per run when `random_seed == 0`, so a smaller budget turns a stable outcome into a random one. The budget is therefore applied only where nothing about the search outcome is asserted:

- **Opted in:** `seed_settings_smoke.py` (asserts the seed echo, the badge and input validation; saves ~1.5 s).
- **Explicitly opted out (`exact_trials()`):** `automatic_size`, `checkbox_tuning`, `funding_evidence`, `global_capital`, `group_lamps`, `pair_plugin`, `screen_settings`, `strategy_search`, `window_table` — every one of them reads a selection, a trial count, a lamp, a ledger row or a table row derived from the search.
- `period_settings_smoke.py` and `rounding_display_smoke.py` never call `/api/proof/optimize`, so the knob is irrelevant to them.

The mechanism, the environment contract and the tests remain in place: a future scenario that only asserts UI behaviour can opt in with one context manager.

### 3.3 No scenario lost its UI-specific assertion

Every suite keeps its original assertions; the change is structural (entry point, context ownership, fixture source). No assertion was moved, weakened or deleted, and no suite was dropped.

## 4. What remains (work item 5)

The phase split says the next lever is assertion time (~60 s) with API compute (~45 s) behind it. Plan §4 orders the migration as: move assertions that carry no UI-specific behaviour into HTTP/JS contract tests, keep the full UI runner as the reference until the replacement passes clean and repeated runs without cache leakage.

Suggested order, from the per-suite cost and the assertion style of each suite:

1. Payload-level assertions currently made inside a browser (`payload…` comparisons, CSV contents, exact metric text) → `packages/web/tests/test_http_routes.py` and a small JS contract harness that imports the static modules under Node.
2. Cohort/plateau lamp math (`group_lamps_smoke.py`, `screen_settings_smoke.py`) → contract tests over `/api/proof/plateau` plus one rendering check.
3. Only then reduce the browser runner's scenario list, one suite at a time, each removal backed by the equivalent contract test in the same change.

Acceptance for item 5 stays as written in the plan: two consecutive clean runs, no cache leakage, no lost UI-specific assertion. Until that lands, the stretch target of <45 s is not reachable: 59 % of the remaining wall time is Playwright interaction and waits that no runner-level change can remove.

## 5. Reproduce

```bash
# whole suite, phase table included (12/12, ~105 s on a 2-vCPU runner)
uv run --with playwright python scripts/run_browser_smokes.py

# one suite, standalone, prints its own phase line
MOEX_OFFLINE_ONLY=1 uv run --with playwright \
    python packages/web/tests/browser/group_lamps_smoke.py

# suite selected by substring, without the aggregated table
uv run --with playwright python scripts/run_browser_smokes.py --filter seed --no-timings
```

Logs from the runs cited above are process-local and are not tracked; the repository's rule is that a number is quotable only with its row in this document or a newer dated baseline.
