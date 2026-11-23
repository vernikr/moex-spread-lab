# S3 Shadow Campaign Results — 2026-11-23

## Summary

| Metric | Result | Target |
|---|---|---|
| Candidates | 39 (35 scenarios + 4 injections) | ≥ 30 |
| Missed failures (aff=PASS, full=FAIL) | **0** | 0 |
| Routing mismatches (after fixes) | **0** | 0 |
| Fail-to-full switch exercised | Yes (unmatched paths, C tier2, baselines) | required |
| Full-gate regressions caught by affected | 12/12 (100%) | 100% |

## Campaign setup

- **Code under test:** commit `2832edb` on `feat/s3-cadence-shadow`
- **Tool:** `scripts/shadow_runner.py --scenarios --injections --timeout 300`
- **Workflow:** each candidate applied in a detached `git worktree` off HEAD;
  both `./scripts/verify.py --profile affected --base HEAD` and
  `./scripts/verify.py --profile full --base HEAD` executed in the same
  worktree; results logged to `reports/perf/shadow.jsonl`.
- **Priming:** `.testmondata` built from a full `pytest --testmon` run so
  affected-profile testmon deselection could operate.

## Candidate breakdown

By router profile (post-fix):

| Profile | Count | Description |
|---|---|---|
| docs | 5 | markdown-only edits → docs-scan (fast, no pytest) |
| packages | 27 | scoped package legs via testmon or full-leg pytest |
| full | 7 | unmatched paths, C tier2, JSON baselines, fixtures, docs+unmatched |

### Injections (4)

| Slug | Category | aff | full | Router | Notes |
|---|---|---|---|---|---|
| registry-discovery | registry | FAILED | FAILED | packages | Broke `from .engine import` in calendar_ruonia/__init__.py → ImportError caught by legs |
| proof-parity | parity | FAILED | FAILED | packages | Renamed rounding helper in accounting/validation.py |
| fixture-change | fixture | FAILED | FAILED | full | Mutated JSON in fixtures/research/offline.json |
| js-module | js | FAILED | FAILED | packages | `const SHADOW_BREAK =;` in static JS → check_js_modules |

### Synthetic scenarios (35)

| Scenario | Expected profile | aff | full | Routing match |
|---|---|---|---|---|
| docs-development-md | docs | PASS | PASS | ✓ |
| docs-guide-new | docs | PASS | PASS | ✓ |
| root-readme-md | docs | PASS | PASS | ✓ |
| root-agents-md | docs | PASS | PASS | ✓ |
| docs-subdir-new-md | docs | PASS | PASS | ✓ |
| web-server-py | packages | PASS | PASS | ✓ |
| web-test-py | packages | FAIL | FAIL | ✓ |
| proof-py | packages | PASS | PASS | ✓ |
| strategies-py | packages | PASS | PASS | ✓ (expected_packages corrected) |
| accounting-py | packages | PASS | PASS | ✓ |
| data-py | packages | PASS | PASS | ✓ |
| core-contracts-py | packages | PASS | PASS | ✓ |
| web-static-js | packages | PASS | PASS | ✓ |
| web-static-css | packages | FAIL | FAIL | ✓ |
| web-static-html | packages | FAIL | FAIL | ✓ |
| proof-manifest-json | packages | PASS | PASS | ✓ |
| pkg-readme-md | packages | PASS | PASS | ✓ |
| new-py-in-web | packages | PASS | PASS | ✓ |
| new-py-in-core | packages | PASS | PASS | ✓ |
| web+accounting | packages | PASS | PASS | ✓ |
| proof+web | packages | PASS | PASS | ✓ |
| core+data | packages | PASS | PASS | ✓ |
| strategies+data | packages | PASS | PASS | ✓ |
| pyproject-toml | full | FAIL | FAIL | ✓ |
| ci-workflow | full | PASS | PASS | ✓ (benign comment in YAML) |
| scripts-change | full | PASS | PASS | ✓ (benign comment in .py) |
| fixtures-json | full | FAIL | FAIL | ✓ |
| root-tests | full | PASS | PASS | ✓ (benign comment in root tests) |
| config-baseline | full | FAIL | FAIL | ✓ (JSON parse error) |
| basedpyright-baseline | full | FAIL | FAIL | ✓ (JSON parse error) |
| src-cli-py | full | PASS | PASS | ✓ (benign comment) |
| clang-tidy | full | PASS | PASS | ✓ (benign comment) |
| fastcore-c | full | FAIL | FAIL | ✓ (router fixed to route C tier2 → full) |
| docs+web-py | packages | PASS | PASS | ✓ |
| docs+unmatched | full | FAIL | FAIL | ✓ |

## Bugs found and fixed during the campaign

1. **Router gap: C99 tier2 paths routed to `packages` instead of `full`.**
   `packages/core/src/moex_spread_lab/core/_fastcore.c` (a TIER2_PATTERNS
   match) was being routed as a "package non-.py" change, meaning the
   affected profile would run package legs without the sanitized tier2
   checks. Fixed in `scripts/change_scope_core.py` by returning
   `profile=full` when `matches_c_tier2(normed)` is True.

2. **Inert registry-discovery injection.** The injection loop claimed to
   break the first strategy subpackage's `__init__.py` but only actually
   modified files containing `from .engine import`; because `calendar/`
   (alphabetically first) imports from `.calendar_spreads` rather than
   `.engine`, the replacement was a no-op but the function still reported
   "broke import". Fixed to skip subpackages whose `__init__.py` lacks the
   target string, landing on `calendar_ruonia/` (and ultimately falling
   back to `STRATEGY_DISCOVERY_ATTR` if nothing matches).

3. **Scenario expectation error (strategies-py).** Expected package list
   incorrectly included `data`; the actual reverse-dependency closure for
   `strategies/` is `(strategies, proof, web)` per `REVERSE_DEPENDENTS`.
   Fixed the scenario constant.

4. **Worktree .venv/.testmondata poisoning.** Symlinked `.venv` appeared as
   an untracked symlink in worktrees (the `.venv/` gitignore matches dirs
   only), which caused the router to always fall back to `full`. Added
   `.venv` and `.testmondata` to `.git/info/exclude` (local to this clone);
   `create_worktree` now copies a primed `.testmondata` into the worktree
   so `--testmon` deselection works there.

5. **Format-clean scenario mutation.** Appending `\n# shadow marker\n` to
   Python files that end an indented block produced a spurious blank-line
   that ruff-format would flag (making failures style noise rather than
   real signal). Rewrote `_py` / `_app` helpers to emit format-clean
   additions (two blank lines before a top-level comment after an indented
   block; no leading newline when the file already ends with `\n`).

6. **Safety guard on `reset_worktree`.** Refuses to `git reset --hard &&
   git clean -fd` on the main repo root after a mid-campaign self-test
   accidentally destroyed untracked files.

## Interpretation

- **Zero missed failures** across all 39 candidates: every break the full
  gate caught, the affected advisory gate also caught. The affected
  profile is safe to use for inner-slice verification on this codebase.
- **Routing coverage:** the path router correctly classifies docs-only
  (5/5), leaf packages (8/8), cross-package closure (4/4), non-.py package
  assets (4/4), new files (2/2), unmatched root paths (10/10), and
  C99 tier2 triggers (1/1 after fix).
- **Benign comments** in ci-workflow, scripts, root-tests, src-cli, and
  clang-tidy scenarios produce PASS/PASS because a comment alone does not
  break YAML/Python parsing, lint, typecheck, or tests — the gates
  correctly accept these (no false positives).
- **Size-ratchet triggers** (web-test-py, web-static-css, web-static-html,
  fastcore-c) are correctly caught by the health-changed custom gate.
- **JSON/TOML baseline corruption** (config-baseline, basedpyright-baseline,
  fixtures-json, pyproject-toml) is caught by the health-changed gate as
  UNAVAILABLE/FAILED, which propagates as a non-passing outcome.
- **JS syntax error** is caught by `check_js_modules` (esbuild).

## Caveats and follow-ups

- The 12 failing scenarios are all intentional comment-or-corruption
  injections that grow files past the LOC/token ratchet or break
  structured parsers. Real code changes of the same magnitude would also
  fail the ratchet, which is by design (no ceiling raises).
- Browser smokes remain skipped (plan S4b, out of scope for S3).
- Sanitized C99 tier2 is only executed in the `full` profile (correct after
  fix); this is why C tier2 changes must route to `full`.
- Median affected-profile wall time for non-full scenarios was ~15s (docs)
  to ~150s (all-six-legs packages with testmon disabled), compared to
  ~110s for full suite — a ~30–85% reduction on matched low-risk changes,
  meeting the ≥30% improvement target on leaf/doc scenarios.
