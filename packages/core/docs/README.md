# `msl-core` (Layer 0) — durable module guide

`packages/core` owns the contracts and primitives every other workspace package
depends on, and it depends on no workspace package itself. Everything here is
offline: no socket, no market-data read, no broker call.

Source: `packages/core/src/moex_spread_lab/core/`. Manifest:
`packages/core/manifest.json`. This guide is a durable contract reference, not a
work plan; live status is in GitHub Issues and Project #2.

## 1. What this package owns

| Area | Module | Durable rule |
|---|---|---|
| Run/evidence manifests | `w0.py` | `run-manifest.v1`, `w0.fixture.v1`, `w0.replay.v1`; canonical JSON bytes, hashing, manifest/artifact validation, secret scanning. |
| File-snapshot cache | `hashed_json_cache.py` | Bounded, thread-safe LRU keyed by immutable file snapshots; every lookup is counted through `cache_metrics.py`. |
| Research settings | `settings.py` | One immutable `ResearchSettings` snapshot per request plus `EvaluationSplit` for training/validation dates. |
| Bar intervals | `timeframes.py` | `TIMEFRAMES` registry (`1h`, `1d`); adding an interval is a registry entry plus data for that exact ISS interval. |
| Universe | `universe.py` | Generic universe IDs (`pair_screen`, `calendar_native`), pair windows, strategy defaults, and contract tickers. |
| Shared contracts | `contracts.py` | `ParameterSpec`, `InstrumentContext`, `WindowSnapshot`, `StrategyIntentRun`, and the `StrategyPlugin` protocol. |
| C99 accelerator | `_fastcore.c`, `_fastcore_loader.py` | Optional bit-for-bit accelerator; the pure-Python path stays authoritative. |
| Diagnostics/support | `cache_metrics.py`, `derived_cache.py`, `evaluation_periods.py`, `json_types.py`, `cli.py` | Supporting runtime; no contract authority. The workstation bridge harness (`moex-spread-lab remote ...`) lives in the root application package `src/moex_spread_lab/remote/`, not in msl-core. |

## 2. Contracts other packages must not silently change

- **Hashing is canonical.** `w0.canonical_bytes` (sorted keys, UTF-8, fixed
  separators) defines every artifact hash, `run_id` input and provenance digest.
  Changing serialization invalidates stored `run_id` values; treat it as a
  contract change with a conformance test, not a refactor.
- **Manifests fail closed.** `validate_manifest` rejects unknown versions,
  missing artifacts, hash drift and network-enabled configurations rather than
  degrading. `secret-scan` is a gate, not a warning.
- **Settings are snapshots, never ambient state.** `read_settings` fills defaults
  for newer fields without rewriting an older file; `save_settings` writes
  atomically through a staging directory, so a concurrent reader sees one complete
  version. Booleans are never coerced from numbers or strings.
- **`EvaluationSplit` reserves a suffix.** The validation share is
  `max(1, floor(n × percent / 100))` sessions when `percent > 0`; requested
  validation is never silently disabled, and unavailable validation is reported
  as unavailable instead of being replaced by training.
- **Universe IDs are protocol, tickers are data.** Universe constants name the
  request shape (`pair_screen`, `calendar_native`); the concrete MOEX catalogs are
  inert data. Neither grants trading authority.
- **The `StrategyPlugin` protocol is the only strategy contract.** Drop-in
  strategies are discovered from their folder and declare their proof adapter
  through the protocol; no registry list, entry-point table or architecture-test
  edit is a contract.

## 3. Boundaries

- Layer 0 imports no other `msl-*` package. `packages/core/tests` must stay
  runnable with `uv run --package msl-core pytest packages/core/tests`, so core
  tests may not import upward layers.
- Market data, cost/risk models, strategies, proof orchestration and HTTP/UI
  belong to `msl-data`, `msl-accounting`, `msl-strategies`, `msl-proof` and
  `msl-web`. If a change needs one of them, it does not belong in this package.
- `ResearchSettings` values are research preferences. They never become
  broker authority, real funding, or a claim of executable returns.

## 4. Environment switches that change behaviour

| Variable | Effect |
|---|---|
| `MOEX_OFFLINE_ONLY=1` | Set in every standalone/browser harness so a cache miss fails closed instead of probing MOEX ISS. |
| `MOEX_PROOF_REFERENCE_EVAL=1` | Disables the accelerated metric fast path for reference comparison. |
| `MOEX_PROOF_DISABLE_C=1` | Forces the `_fastcore` pure-Python path. |
| `MOEX_PROOF_WORKERS=1` | Single-threaded reference evaluation. |

## 5. Verification

```sh
uv run pytest packages/core/tests -q
uv run python scripts/architecture_gate.py            # DAG, discovery, manifests, secret scan
uv run pytest -q -n auto --dist loadfile              # full package-scoped suite
```

Touching `_fastcore.c` or its pure-Python mirror also requires
`packages/proof/tests/test_perf_equivalence.py` (and
`scripts/run_sanitized_pytest.py` when clang is available).
