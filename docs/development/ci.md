# CI and fast test runs

Continuous integration runs on every push to `main` and on pull requests (`.github/workflows/ci.yml`). It exists to keep the proof-search contracts green; it deploys nothing and does not touch data caches.

## What the workflow does

- `astral-sh/setup-uv` installs uv with the cache enabled and keys it on `uv.lock`, so dependency resolution is reproducible and incremental.
- `uv sync --frozen` installs the locked environment. `pyproject.toml` declares a `dev` dependency group (`pytest`, `pytest-subtests`, `pytest-xdist`) that uv installs by default, so tests no longer need ad-hoc `--with` flags.
- Tests run with `uv run pytest -q -n auto --dist loadfile --durations=15`. The suite passes under pytest-xdist (`loadfile` keeps each file on one worker); bit-for-bit `perf_equivalence` tests stay valid because workers get separate interpreter processes.
- `concurrency` cancels superseded runs on the same ref. No macOS runner is used yet; add one only if the C99 accelerator or path handling regresses specifically there.
- `UV_COMPILE_BYTECODE=1` removes first-import bytecode compilation from the critical path.

## Local fast run

```sh
uv sync --frozen
uv run pytest -q -n auto --dist loadfile
```

Expected wall time is roughly half the sequential run (~33 s vs ~64 s on a 2-core machine). For the exact sequential reference path, run `uv run pytest -q`.

## Diagnostics

- `--durations=15` prints the slowest tests; keep an eye on `tests/test_perf_equivalence.py` and `tests/test_tuning_screens.py`, which dominate runtime.
- The reference-equivalence tests honor `MOEX_PROOF_REFERENCE_EVAL=1` and `MOEX_PROOF_DISABLE_C=1`; use them when triaging numeric drift, not in CI.
- If xdist ever flakes, fall back to the sequential command above and file the ordering dependency as a bug; do not mask it by disabling `perf_equivalence`.
