# CI and fast test runs

Continuous integration runs on every push to `main`, on pull requests, weekly (Monday 04:17 UTC) and on manual dispatch (`.github/workflows/ci.yml`). It deploys nothing and never touches data caches.

## Layout

- `test` matrix, shard `1/2` and `2/2` (ubuntu-latest): the functional suite, split with pytest-split (`least_duration`, `--splits 2`) over committed `.github/test-durations.json`. Each shard runs `pytest -n auto --dist loadfile` on its own runner; grouping is balanced to within ~1 s (≈14 s per shard).
- `perf-equivalence` (ubuntu-latest): `tests/test_perf_equivalence.py` alone, so the bit-for-bit contract still gates merges without inflating the functional shards.
- Every job: `astral-sh/setup-uv` with the uv cache keyed on `uv.lock`; `uv sync --frozen` (the locked `dev` group installs `pytest`, `pytest-subtests`, `pytest-xdist`, `pytest-split`, so CI and local runs are identical); then a hard fail if the C99 `_fastcore` module cannot compile (no silent Python fallback in CI).
- `concurrency` cancels superseded runs on the same ref.
- Each job tees `--durations=15` output and uploads it as an artifact (7-day retention): `durations-shard-1`, `durations-shard-2`, `durations-perf-equivalence`.

## Other workflows

- `.github/workflows/ci-macos.yml` — opt-in (manual dispatch, weekly Tuesday). Full suite on `macos-latest` to catch clang/path drift in the fastcore; not per-push because macOS minutes bill at 10x on private repositories.
- `.github/workflows/browser-smoke.yml` — Playwright Chromium suites over synthetic data (`tests/browser/*_smoke.py`), manual dispatch and weekly Wednesday. Verified headless on Linux; all ten suites pass in ~2 min. Kept out of per-push CI on purpose.

## Local fast run

```sh
uv sync --frozen
uv run pytest -q -n auto --dist loadfile
```

Expected wall time is roughly half the sequential run (~33 s vs ~64 s on a 2-core machine). For the exact sequential reference path, run `uv run pytest -q`.

Refresh the split durations after changing test weighting (roughly monthly, or when shards drift apart):

```sh
uv run pytest -q -n auto --dist loadfile --store-durations --durations-path .github/test-durations.json
git add .github/test-durations.json
```

## Diagnostics

- `--durations=15` keeps the slowest offenders visible (`tests/test_perf_equivalence.py`, `tests/test_tuning_screens.py`); CI uploads them per run.
- The reference-equivalence tests honor `MOEX_PROOF_REFERENCE_EVAL=1` and `MOEX_PROOF_DISABLE_C=1`; use them when triaging numeric drift, not in CI.
- If xdist ever flakes, fall back to the sequential command above and file the ordering dependency as a bug; do not mask it by disabling `perf_equivalence`.
