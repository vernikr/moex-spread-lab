# CI and fast test runs

Continuous integration runs on every push to `main`, on pull requests, weekly (Monday 04:17 UTC) and on manual dispatch (`.github/workflows/ci.yml`). It deploys nothing and never touches local market-data caches.

## Layout

- `architecture-gate` (ubuntu-latest) runs first. After syncing the locked workspace, `scripts/architecture_gate.py` checks the full package/import DAG, the no-legacy-shim rule for internal code/tests, built-in and synthetic strategy discovery, `validate-manifest`, and `secret-scan --tracked-only`. The gate script is dependency-light and targets under 2 seconds; environment setup is outside that measured step.
- `package-tests` is a six-member matrix (`core`, `data`, `accounting`, `strategies`, `proof`, `web`). Each job runs only `packages/<member>/tests` with `pytest -n auto --dist loadfile`, and uploads its `--durations=15` report as a seven-day artifact. Parallel package jobs avoid a single shared duration JSON; xdist schedules whole files dynamically within each package.
- `perf-equivalence` runs `packages/proof/tests/test_perf_equivalence.py` separately so bit-for-bit numerical and hash parity remains a merge gate.
- Every job uses `astral-sh/setup-uv`, caches by `uv.lock`, and runs `uv sync --frozen`. Test jobs also fail if the C99 `_fastcore` extension cannot compile (there is no silent Python fallback in CI).
- `concurrency` cancels superseded runs on the same ref.

Pytest's `testpaths` includes only the six package test directories. Root `tests/` contains shared synthetic fixture modules used by package and browser tests; it is importable through pytest's `pythonpath` but is not independently collected.

## Other workflows

- `.github/workflows/ci-macos.yml` — opt-in (manual dispatch, weekly Tuesday). Runs the same architecture gate and full package-scoped test suite on `macos-latest` to catch clang/path drift; it is not per-push because macOS minutes bill at a 10x multiplier on private repositories.
- `.github/workflows/browser-smoke.yml` — manual dispatch and weekly Wednesday. Runs the architecture gate, installs Playwright Chromium, then runs every synthetic-data script in `packages/web/tests/browser/*_smoke.py`. Browser startup remains out of per-push CI.

## Local runs

```sh
uv sync --frozen
uv run python scripts/architecture_gate.py
uv run pytest packages/proof/tests -q
uv run pytest -q -n auto --dist loadfile
```

Use a package-specific path for focused work (for example `packages/strategies/tests`). The full suite uses the six configured `testpaths`; no central duration file needs refreshing or committing. For the sequential reference path, run `uv run pytest -q`.

## Diagnostics

- `--durations=15` keeps slow tests visible in CI artifacts; the bit-for-bit equivalence suite remains isolated in its own job.
- The reference-equivalence tests honor `MOEX_PROOF_REFERENCE_EVAL=1` and `MOEX_PROOF_DISABLE_C=1`; use them to diagnose numeric drift, not as CI defaults.
- If xdist ever flakes, reproduce sequentially and file the ordering dependency as a bug; do not mask it by disabling `perf_equivalence`.
