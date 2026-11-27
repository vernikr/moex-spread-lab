# `msl-core` (Layer 0) — contract reference

`packages/core` owns the primitives every other workspace package imports and imports no
`msl-*` package itself. Everything is offline: no socket, no market-data read, no broker call.
Live status lives in GitHub Issues and Project #2, not here.

| Module | Contract |
|---|---|
| `w0.py`, `cli.py` | `canonical_bytes` (sorted keys, UTF-8, compact separators, NaN rejected), `sha256_bytes` and `sha256_file` are defined in `derived_cache.py` and re-exported here; they feed every artifact hash, and `run_id` is the first 16 hex digits of its SHA-256, so changing the serialization is a contract change with a conformance test. The JSON boundary (`JSONBoundaryError`, `as_str_dict`, `load_object`) narrows decoded input at the system edge. `scan_secrets` fails on one finding and ships as `moex-spread-lab secret-scan [--root] [--tracked-only]`. `load_contracts` reads ISS-intake instrument facts; `read_hashed_json` serves the shared hashed-JSON LRU. |
| `settings.py` | `ResearchSettings` is an immutable snapshot with a validator per field: percent 0-50, capital 0.01-1e9 RUB with two decimals, slippage 0-10 ticks, registered timeframe, known price fill, seed 0-2^31-1 (0 draws a fresh seed per run); booleans are never coerced. `read_settings` fills defaults an older file lacks without rewriting it; `save_settings` merges a partial patch and replaces the file atomically. `EvaluationSplit.from_dates` reserves the last `max(1, floor(n * percent / 100))` sessions for validation when `percent > 0`; a requested check is never silently disabled. Values are research preferences, never broker authority or a claim of executable returns. |
| `timeframes.py` | `TIMEFRAMES`: `1h` (ISS interval 60, intraday, no funding strategy) and `1d` (ISS interval 24, default). Adding one is a registry entry plus data for that exact interval. |
| `universe.py` | Universe IDs `pair_screen` / `calendar_native`, built-in `STRATEGIES` defaults and the capital, volume and quantity limits; catalog names (`PAIRS`, ...) resolve lazily to `moex_spread_lab.data.universe`. IDs are protocol, tickers are data, neither grants trading authority. |
| `contracts.py` | `ParameterSpec`, `InstrumentContext`, `WindowSnapshot`, `StrategyIntentRun`, `EntryBandPoint`, `CompatibilityVerdict`, the `StrategyPlugin` protocol and the `plugin_*` accessors. The protocol is the only strategy contract: plugins are discovered from their folder in `msl-strategies`. |
| `derived_cache.py` | JSON cache under `data/analytics/derived-cache/` keyed by a fingerprint (`msl.derived-cache/2`) of `data/` (minus `data/analytics`), `data-lake/`, `packages/*/src/` and `src/`. The key is the freshness proof; a corrupt entry is a miss. Also the canonical home of the shared hashing primitives (`canonical_bytes`, `sha256_bytes`, `sha256_file`), the thread-safe `HashedJSONCache` LRU and the opt-in `collect_cache_metrics` / `record_cache_lookup` telemetry. |
| `hashed_json_cache.py`, `cache_metrics.py`, `json_types.py` | One-line re-export stubs for Post-Wave 1 merge safety; canonical implementations live in `derived_cache.py` / `w0.py` (stubs retired by #246). |
| `_fastcore.c`, `_fastcore_loader.py` | Optional C99 accelerator built on first import; the pure-Python path stays authoritative and `packages/proof/tests/test_perf_equivalence*.py` pins bit-for-bit parity. `clean_obj` recursively replaces non-finite floats with `None` (C99 `fc.clean_obj` when built, else the `math.isfinite` fallback). |

`test_architecture_dag.py` enforces that Layer 0 imports no other `msl-*` package at import
time and that `packages/core/tests` imports no upward layer.

## Environment switches read here

| Variable | Effect |
|---|---|
| `MOEX_PROOF_DISABLE_C=1` | `load_fastcore()` returns `None`: pure-Python path only. |
| `MOEX_FASTCORE_SANITIZE=1` | Builds `_fastcore_asan` under clang ASan/UBSan; needs the runtime preloaded by `scripts/run_sanitized_pytest.py`. |
| `MOEX_DERIVED_CACHE=0` | Bypasses the derived cache. |
| `MOEX_HASHED_JSON_CACHE_CAPACITY` | Hashed-JSON LRU capacity (default 256). |

## Verification

```sh
uv run pytest packages/core/tests -q
uv run python scripts/architecture_gate.py      # DAG, plugin discovery, package manifests, secret scan
./scripts/verify.py --profile full
uv run python scripts/run_sanitized_pytest.py   # when _fastcore.c or its Python mirrors change
```
