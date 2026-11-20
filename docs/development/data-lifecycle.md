# Local market-data inventory and retention (Phase 0)

## Purpose

Phase 0 adds a read-only inventory before any storage migration or cleanup. It makes the ignored local `data/` tree observable without changing downloaded market data, caches, research results, or runtime behavior.

The inventory is a planning and evidence aid only. A path marked `archive_candidate` or `rebuildable_cache` is **not** an authorization to delete it. Preserve a pinned source/dataset manifest for any result that must remain reproducible.

## Command

From the repository root:

```sh
# Fast size-and-policy status; does not parse JSON payloads or write a file.
uv run python -m moex_spread_lab.cli data-inventory --root . --fast

# Full manifest: schema, row counts, date ranges and SHA-256 of every local file.
uv run python -m moex_spread_lab.cli data-inventory \
  --root . \
  --hashes \
  --output data/analytics/data-inventory.json
```

The command writes only when `--output` is explicitly supplied. Otherwise it prints a compact JSON summary. The optional output remains under ignored `data/`; do not commit local data manifests that expose workstation paths or inventory.

## Classifications

| Classification | Meaning | Current retention posture |
|---|---|---|
| `active_input` | Offline reader consumes it directly. | Keep while the corresponding research branch is active. |
| `derived_runtime` | Local UI settings or short run history. | Keep with rotation. |
| `rebuildable_cache` | Intake cache, not a screener runtime input. | TTL or rebuild after an explicit review. |
| `archive_candidate` | Legacy/unregistered timeframe or discovery artifact. | Pin relevant runs, then archive rather than delete. |
| `derived_archive_candidate` | Detailed generated analysis result. | Keep summary and run manifest; archive detail. |
| `unclassified` | No policy registered. | Review manually; never delete automatically. |

The current policy treats 1d and 1h `w1/candles/` series as active because those are the registered runtime timeframes. Other timeframe outputs, `candles-common`, and discovery artifacts are candidates for archival only. FORTS history caches are classified as active inputs because offline funding proof reads them.

## Manifest contents and limits

The full inventory records each file's relative path, logical and allocated bytes, mtime, policy, schema/timeframe metadata where available, row count/date range where discoverable, and optional SHA-256. It also aggregates datasets and reports potential reclaimable bytes for candidates. That byte total intentionally requires review; it is not a deletion plan.

The scanner does not follow symbolic links and does not emit source URLs or credential values from local JSON. JSON inspection can be expensive for multi-gigabyte data trees; use `--fast` for routine status and `--hashes` only when preparing a pinned snapshot.

## Phase 1: verified rebuildable-cache archive

Phase 1 is deliberately narrower than a general cleanup: it archives only files classified as `rebuildable_cache` by a pinned, SHA-256 inventory. The archive must live outside `data/`, contains an embedded manifest, and every member hash is verified before any source file is removed. Active inputs and `archive_candidate` datasets are never selected by this command.

```sh
# Inspect the exact destructive plan. This changes nothing.
uv run python -m moex_spread_lab.cli data-archive \
  --root . \
  --manifest data/analytics/data-inventory.json \
  --archive ../moex-spread-lab-archives/rebuildable-cache-YYYYMMDD.tar.gz

# Only after reviewing the plan, create and verify the archive, then remove matching cache files.
uv run python -m moex_spread_lab.cli data-archive \
  --root . \
  --manifest data/analytics/data-inventory.json \
  --archive ../moex-spread-lab-archives/rebuildable-cache-YYYYMMDD.tar.gz \
  --apply
```

The command rejects a stale manifest, a missing or changed source, an existing archive destination, an archive path inside `data/`, symbolic links, and a manifest without file hashes. If archiving fails before verification, no source is deleted. If a source cannot be deleted after a verified archive exists, the verified archive remains the recovery copy and the command reports the failure.

## Phase 2: Parquet pilot store

Phase 2 adds a typed local store beside, not instead of, legacy JSON. `data-lake/` is ignored local state. It contains Zstandard-compressed Parquet bars, a deduplicated Parquet source-page manifest, `manifest.json`, and a DuckDB catalog. No screener reader is switched in this phase and no legacy candle JSON is removed.

The v1 bars schema preserves the legacy timestamp strings and IEEE-754 `float64` OHLCV values. This intentionally prioritizes migration equivalence; integer-tick or decimal storage is a later, separately tested change. Repeated `secid`, timeframe, interval, proxy assumption and source URL/hash are moved out of every bar. `source_page_id` joins a bar to its one-per-page provenance row.

```sh
uv run python -m moex_spread_lab.cli data-import-candles \
  --root . \
  --legacy data/w1/candles/CNYRUBF_1d.json \
  --legacy data/w1/candles/CRZ6_1d.json \
  --legacy data/w1/candles/CNYRUBF_1h.json \
  --legacy data/w1/candles/CRZ6_1h.json \
  --verify
```

The importer refuses an existing dataset unless `--replace` is explicit. `--verify` compares Parquet bars and source-page provenance with the legacy JSON and checks the DuckDB catalog plus recorded SHA-256 values.
