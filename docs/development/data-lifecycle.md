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

## Phase 3: verified read-path cutover

`MarketDataStore` is the narrow compatibility adapter used by the screener, its audit, funding timeline and hourly calendar reader. It returns `BarSeries`, so business logic receives normalized bars rather than a JSON `Path`.

A Parquet dataset is selected only when both conditions hold: its manifest names the exact legacy JSON file selected by the old lookup order, and the current file SHA-256 equals the manifest's `legacy_sha256`. The resulting series deliberately reports that legacy path and hash in provenance, preserving existing run identities. A different `candles-screen` copy, a changed legacy JSON, an unimported series, or a missing data lake remains on `LegacyJsonBarStore` automatically.

The Phase-2 CNYRUBF/CRZ6 pilot can therefore accelerate matching `data/w1/candles` inputs without changing a preferred `candles-screen` source. Legacy JSON remains the compatibility input and is not removed in this phase.

Callers may request the columns they need; Parquet reads use PyArrow column projection while the JSON compatibility reader applies the same logical projection. Series-level price-proxy semantics are restored from the verified manifest so this physical optimization does not change normalized output.


## Phase 4: incremental ingestion, raw retention and maintenance

`data-refresh` keeps the retained legacy JSON compatibility file and its verified Parquet mirror in lockstep. For an existing dataset it reads the recorded watermark and requests only the observed-session overlap before that watermark through `--till-date`; the default overlap is five sessions. If the watermark already reaches `--till-date`, it makes **zero** network requests. A first backfill (or `--full-refresh`) needs `--from-date`.

```bash
# First dataset: bounded to two ISS workers and 90-day request chunks.
uv run python -m moex_spread_lab.cli data-refresh \
  --root . --secid CNYRUBF --interval 60 \
  --from-date 2026-01-01 --till-date 2026-11-20

# Incremental run: derives the start from the verified watermark.
uv run python -m moex_spread_lab.cli data-refresh \
  --root . --secid CNYRUBF --interval 60 --till-date 2026-11-21
```

Fresh ISS bodies are retained under `data-lake/raw/` as exact Zstandard-compressed `.json.zst` files. The raw manifest records canonical request SHA-256, response SHA-256, received time, TTL and pin state; its identity includes all three hashes so repeated identical responses remain separately auditable. Default retention is 30 days; `--pin-raw` protects bodies produced by an important run. Use `data-pin-raw --entry-id …` later to pin a registered response. When a lake dataset is replaced, the prior dataset record is appended to `manifest.json`'s `dataset_revisions` history rather than being silently discarded.

```bash
# Integrity verification includes Parquet, source-page provenance, DuckDB catalog and raw bodies.
uv run python -m moex_spread_lab.cli data-verify --root .

# Existing imports are already a single Zstd file per dataset; force performs a verified rewrite.
uv run python -m moex_spread_lab.cli data-compact --root . --force

# Collection is non-destructive by default. Only --apply removes expired unpinned raw files/orphans.
uv run python -m moex_spread_lab.cli data-gc --root . --dry-run
uv run python -m moex_spread_lab.cli data-gc --root . --apply
```

The refresh takes an exclusive per-dataset writer lock before reading the watermark and holds it through fetch, publish and verification. It merges on the candle `begin` key, sorts deterministically, atomically publishes the JSON file, upserts its mirror and then runs the same legacy-versus-Parquet verification used by the importer. It does not delete legacy JSON or alter active screen-first source selection.
