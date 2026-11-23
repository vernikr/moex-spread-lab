# S4b — Browser smoke cost: measurement and implementation (2026-11-23)

**Status:** all five work items landed (1–4 in `1be0a14`, item 5 in §4). Item 5 moved the payload-level assertions out of the browser suites to contract tests that already owned those facts and added the one contract the browser check was silently relying on; the stretch target of <45 s is still not reachable and §4 says why with the measured phase split. Sibling slice of [plan.md](plan.md) §4 (S4b). Numbers below are measured, not estimated; every row states its sample count.

---

## 1. What changed

| Work item | Implementation |
| --- | --- |
| 1. Phase timers first | `packages/web/tests/browser/_harness.py` accumulates `template_build`, `fixture_clone`, `server_startup`, `browser_launch`, `app_startup`, `api_compute`, `static` and prints one `MOEX_SMOKE_PHASES={…}` line per scenario; the remainder is reported as `ui_assertions`. `scripts/run_browser_smokes.py` aggregates them into a table after the run (`--no-timings` opts out). API compute is measured inside the loopback handler, so it is server time, not round-trip time. |
| 2. Shared browser, isolated contexts | `scripts/run_browser_smokes.py` runs one worker process per job, launches Playwright and Chromium once per worker (lazily, on the first suite that needs them) and calls each suite's `scenario(browser)` entry point. Every scenario keeps its own `BrowserContext` and loopback server: `_harness.scenario_isolation(browser)` records the contexts a scenario creates and closes them even when an assertion fails, so a half-finished suite cannot leak pages into the next one. `_harness.standalone(scenario, MESSAGE)` keeps `python <suite>_smoke.py` working for one-off runs; both entry points share the same code path. |
| 3. Fixture templates | `packages/web/tests/browser/_templates.py` builds a template once per worker, keyed by builder identity (module + qualified name + a fingerprint of code, defaults and captured values, so two closures from one factory never collide), and clones it into each scenario's temporary root. Mutation in a scenario (funding-rate rewrites, candle shocks) therefore never reaches the template. |
| 4. Bounded trial budget | `packages/proof/src/moex_spread_lab/proof_runtime/trial_budget.py` owns `TRIALS_PER_STRATEGY = 80` and `budget()`: `MOEX_UI_SMOKE_TRIALS` is honoured only under `MOEX_OFFLINE_ONLY=1` and only downwards. Suites opt in with `_harness.ui_only_trials()` and opt out explicitly with `_harness.exact_trials()`. Only `seed_settings_smoke.py` opts in (§3.2). |
| 5. Contract-test migration | Every payload-level assertion that had no UI-specific content was removed from the browser suites and replaced by a comment naming the contract test that owns it; the client-side CSV export (the only browser check with no server counterpart) got `packages/web/tests/test_ledger_csv_contract.py`. §4 lists each move. |

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

Shares overlap by construction (`app_startup` includes the first proof compute of a page, and `ui_assertions` is the clamped remainder), so they are read as "where the time is", not as a partition. The two post-item-5 runs measured `ui_assertions` 66.9 and 67.7 s and `api_compute` 41.7 and 51.7 s, i.e. the same shape after the migration.

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

For items 1–4 the change was structural (entry point, context ownership, fixture source): no assertion was moved, weakened or deleted, and no suite was dropped. Item 5 then moved the payload-level assertions listed in §4.2, each to a contract test that already asserted the same fact at a lower layer; every UI-specific assertion stayed.

## 4. Work item 5: moving the payload assertions down a layer

### 4.1 Audit first

The plan's order was "assertions that carry no UI-specific behaviour go to HTTP/JS contract tests". The audit found that almost none of them needed a *new* test: the facts they asserted are already asserted at the payload level by `packages/proof/tests` and `packages/web/tests`, using the same synthetic fixtures (`tests/funding_fixtures.py`, `tests/proof_fixtures.py`) and the same request dictionaries. The duplication was real, not hypothetical: `test_proof_funding.py::test_weekend_candles_stay_in_price_journal_but_no_monday_double_booking_or_synthetic_settles`, for example, runs the very scenario `funding_evidence_smoke.py` re-asserted through a downloaded CSV.

So item 5 became: delete the duplicates, keep a comment naming the owner, and add a test only where a browser check was the *only* guard.

### 4.2 What moved

| Suite | Removed from the browser | Owner of that fact |
| --- | --- | --- |
| `funding_evidence_smoke.py` | per-day CSV values: `funding_rate_kind`, zero rate/funding, `funding_left_date`/`funding_right_date`, mirrored left/right rates, weekend `no_clearing` rows, `trial_count == 0` | `packages/proof/tests/test_proof_funding.py` (`test_constant_midpoint_1_2_3_and_observed_zero_both_signs`, `test_weekend_candles_…`, `test_cutoff_and_training_tail_never_use_right_anchor_from_holdout`), `test_strategy_search.py::test_universally_bad_members_need_confirmation_before_any_fit` |
| `period_settings_smoke.py` | `payload.evaluation.observations` (42/13), the 1-observation floor at 0.5 %, the split after reload | `test_proof_search.py::test_0_and_50_share_change_result_not_history_or_signals`, `test_global_capital.py::test_default_migration_and_partial_saves_preserve_all_other_preferences` |
| `screen_settings_smoke.py` | the full eight-key `/api/settings` echo, excluded-pair identities for both combinations, search `status`, `trial_count == 0`, `member_count` | `test_global_capital.py::test_default_migration_…`, `test_tuning_screens.py::test_all_four_combinations_in_preflight_search_report_and_live_lamps`, `test_proof_search.py::test_http_home_alias_new_search_rerun_and_local_origin_boundary` |
| `group_lamps_smoke.py` | plateau arithmetic (`by_window_pp`, `utility`), `member_count`, search `status`, the excluded-cohort length | `test_proof_plateau.py::test_mean_at_center_and_every_neighbour_precedes_the_stability_ratio`, `test_incompatible_or_invalid_center_is_not_profiled_on_a_subset_or_clamped`, `test_strategy_search.py` |
| `strategy_search_smoke.py` | `trial_count`/`candidates` accounting, `run_id` echo, the per-search fill set, `cash_shortfall_rows`, `annualized_excess_pp is None` | `test_strategy_search.py::test_funding_can_replace_mr_with_a_full_budget_for_each_and_native_execution`, `test_strategy_search.py::DashEvidenceTests::test_entered_trade_can_later_have_an_unfundable_mark_never_a_zero_rate` |

Kept in the browser, because nothing else can assert it: lamp/table/`#compatibility-list` rendering, the checked cohort surviving `Escape`, the dialog's own controls and validation, the CSV *download* and its client-side column mapping, the dashed chart line, `pageerror` emptiness.

### 4.3 The one gap the audit found: the client-side CSV

The export has no route of its own — `ledger.js::downloadCsv()` writes the 38-column header, the `# …` preamble and one row per `series` entry in the browser — so no Python test could see it, and after §4.2 the browser only checked that the download happens and the columns exist. `packages/web/tests/test_ledger_csv_contract.py` closes that without a JS test runner: it reads `downloadCsv()`'s source, extracts the column list and the `state.payload.<field>` preamble references, and asserts each of them against a live `POST /api/proof/run` payload (series row, ledger row, or trade). A rename on either side now fails a 0.8 s test instead of silently emptying a column.

Writing it found a real defect: the `contracts_held` column reads `l?.quantity`, and no ledger row carries `quantity`, so that column has always exported empty (the value lives on the trade, not the day). Fixing it is a UI change with its own decision about which contract count is meant, so it is pinned as the single allowed gap in `KNOWN_EMPTY` rather than fixed here: the test fails if a second column starts reading a missing field, and fails again once `contracts_held` is repaired and the entry goes stale.

### 4.4 Measured effect

| Tree | Samples | Result | Wall time |
| --- | --- | --- | --- |
| After items 1–4 (`1be0a14`) | n=3 | 12/12 | 104.1 / 109.3 / 112.9 s |
| After item 5 | n=2 consecutive | 12/12 each | 108.7 / 119.5 s |

Per suite the removals are not visible at all, which is itself the honest result of the measurement: `funding_evidence_smoke.py` 3.4–3.9 s → 3.7 s, `screen_settings_smoke.py` 6.3–7.4 s → 6.3 s, `period_settings_smoke.py` 6.4–7.3 s → 7.1 s, `group_lamps_smoke.py` 13.5–14.3 s → 14.8 s, `strategy_search_smoke.py` 14.2–15.3 s → 16.3 s — every delta inside the single-run noise of this runner, in both directions. Deleting an `assert` on a dictionary the suite has already fetched costs microseconds, and the suites still pay for their browser interactions and their proof compute. **The <45 s stretch target is therefore not reached, and item 5 alone cannot reach it**: after the migration `ui_assertions` is still ~67 s and `api_compute` ~42–52 s of a 109–120 s run, and both are the work the suites exist to do. The 120 s ceiling holds but with almost no margin on this sandbox: the second run was 10 % slower suite-for-suite (`checkbox_tuning` 17.4 → 20.9 s, `strategy_search` 16.3 → 18.5 s) with no code change between them, which is the same contention effect that made the `jobs=2` experiment fail in §3.1.

What item 5 did buy is the thing the plan asked of it: the browser suites now assert only what a browser can be wrong about, so a future reduction of the *scenario list* (§4.5) is a documented decision rather than a coverage loss.

### 4.5 If the target must be met

The remaining levers are no longer assertion-shaped, and each trades real coverage, so none of them is taken here:

1. Drop whole scenarios whose subject is a payload contract — `funding_evidence_smoke.py` and `period_settings_smoke.py` are the cheapest candidates (~11 s together) and would lose the only check that the table and journal render those rows at all.
2. Run the suite in CI only (S4b's original framing: a scheduled job) and keep the local pre-commit gate free of it.
3. Spend money on hardware: the runner is 2 vCPU/1 GB and cannot use `jobs=2` (§3.1).

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
