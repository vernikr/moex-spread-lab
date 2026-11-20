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

## Phase 5: compact research run artifacts

Analytical baseline and sensitivity runs are evidence artifacts with a compact control plane and large detail plane. `analytical-baseline --output-dir …` atomically publishes a *new* run directory containing `summary.json`, `daily.parquet`, `trades.parquet` and `manifest.json`. The manifest is `research-run-artifact.v1`; it records the run/artifact ID, input/evidence hashes, component SHA-256 hashes, Parquet row/column counts and detail retention state. Publication stages all files beside the target and refuses to replace an existing directory.

`summary.json` retains the reusable research facts: result schema, release state, pair/timeframe identity, parameters, costs/economics, summary measures and input provenance. It intentionally excludes detailed bars and trades. `daily.parquet` and `trades.parquet` use Zstandard compression. The CLI only writes the older monolithic pretty JSON when `--legacy-json` is explicitly supplied. Existing `execution-ledger` and `validate-analytics` consumers accept a verified run directory as well as that legacy JSON format.

Each new artifact gets a 30-day detail TTL by default; use `--retention-days N` to set it for that run and `--pin` for evidence that must retain its details indefinitely. Collection never removes `summary.json` or `manifest.json`, and it does nothing until `--apply` is explicit:

```bash
# Review only; default root is data/analytics/runs under --root.
uv run python -m moex_spread_lab.cli run-artifact-gc --root . --dry-run --format json

# Pin an important run, or later restart its configured TTL.
uv run python -m moex_spread_lab.cli run-artifact-pin \
  --artifact-dir data/analytics/runs/cnyrubf-crz6-1d-baseline --pin
uv run python -m moex_spread_lab.cli run-artifact-pin \
  --artifact-dir data/analytics/runs/cnyrubf-crz6-1d-baseline --unpin

# Only after reviewing the dry-run plan, collect expired and unpinned detail files.
uv run python -m moex_spread_lab.cli run-artifact-gc --root . --apply --format json
```

After collection the manifest records `detail_status=collected`; the compact result and its provenance remain inspectable, but consumers which require `daily`/`trades` fail closed rather than inventing a replay. A pin protects details from collection. Integrity verification happens automatically when a compact artifact is read: its summary and each required detail file must match the manifest SHA-256 and recorded table shape.

## Storage optimization package A (0.4.0)

`data-archive-candidates` is separate from the older rebuildable-cache command.
It accepts only its own versioned dry-run plan, never globs or an arbitrary list
of paths. Older ad-hoc November 20 plans must be regenerated and compared before
approval. Planning hashes the inventory and deterministically selects the five
reviewed groups: analytics/derived, common candles, 10m/unknown candles and
perpetual-history discovery. Analytics selection is deliberately narrower than
inventory classification: only top-level schema-bearing JSON reports, excluding
`data-*`/`archive-*` control files, runtime histories/settings and compact runs.
Active H1, screened candles, benchmark, funding/calendar caches and unclassified
files are never selected. No active reader contract changes in this package.

```sh
# Use fresh paths outside data/. Planning never overwrites an earlier plan.
uv run moex-spread-lab data-archive-candidates --root . --dry-run \
  --plan ../archives/candidates-plan.json --archive ../archives/candidates.tar.zst

# Only after separate review/approval; does NOT delete originals.
uv run moex-spread-lab data-archive-candidates --root . --archive-only \
  --plan ../archives/candidates-plan.json

# Separate destructive step, only in a maintenance window with writers stopped.
uv run moex-spread-lab data-archive-candidates --root . --apply \
  --plan ../archives/candidates-plan.json
```

The archive embeds `manifest.json` (paths, logical bytes, SHA-256, source
fingerprint, commit and UTC creation time). Zstandard level 19 compression is
streamed with a checksum, into a same-directory staging file. Full streamed
member verification rejects missing/extra/duplicate/non-regular members and
manifest/size/hash mismatches. Sources are revalidated before atomic no-clobber
publication. A `.verified.json` sidecar records the archive SHA-256 and embedded
manifest; apply requires it and repeats full archive and source verification.
An existing destination is never overwritten.

Apply creates a `.apply.json` journal before deleting anything, records deleted
paths and completion/interruption, and prunes empty parents only at the end.
It checks **all** sources before the first unlink and each source again immediately
before unlink. An advisory repository lock excludes concurrent invocations of
this CLI; it cannot stop other applications writing files. Use a maintenance
window. Filesystem failure or external mutation during deletion can leave a
partial operation; this is not a filesystem transaction. The verified archive
remains the recovery source. A hard process kill can leave the journal at
`prepared`; reconcile remaining paths against the manifest rather than retrying
blindly. No automatic resume or automatic production cleanup is implemented.

Restore into a **new empty staging directory**, not over the running checkout:
`tar --zstd -xf candidates.tar.zst -C RESTORE_DIR` (with GNU tar/zstd), then compare
all restored `data/<path>` sizes and SHA-256 values to `manifest.json`. Review
before copying back to the original paths. Keep the archive, verification
receipt, plan and apply journal together; schedule a review before any archive
retention decision. Moving an archive within one disk is not itself a saving.

Validation: isolated-fixture archive/apply/restore, protected inputs, stale
sources, path traversal, symlinks, corruption, malformed members and publication
races are covered. The approved workstation operation is recorded in
[Package A application evidence, 2026-11-21](data-archive-2026-11-21.md): 1,169
files archived, fully restore-verified and removed; 634.35 MiB conservative net
allocated reduction. The old plan's inventory control file was deliberately
excluded. Package B's importer is described below; active H1 Parquet-only
cutover (package C) is not implemented.

## Package B: verified legacy baseline import (0.5.0)

```sh
uv run moex-spread-lab run-artifact-import \
  --legacy RESTORE_DIR/data/analytics/report.json \
  --expected-sha256 HASH_FROM_VERIFIED_ARCHIVE_MANIFEST \
  --output-dir data/analytics/runs/legacy-report
```

Only `analytical-baseline.v1` is accepted. Other schemas, including calendar
studies, stay losslessly archived; there is no generic schema conversion.
Import never deletes or edits the source. When the source was archived by
package A, first verify that archive and restore needed members into temporary
staging, checking each against its manifest. Do not permanently restore legacy
pretty JSON into `data/`. The importer checks `--expected-sha256` when supplied;
it does not itself authenticate an archive or extract it.

Before publication, import freezes the exact source bytes and creates the four
Phase-5 files in private staging. It requires canonical JSON equality of the
**complete** rehydrated report, including all daily/trade rows, summaries,
parameters, provenance, final X and drawdown. It rejects duplicate JSON keys,
non-finite numbers and any lossy Arrow inference (including dropped sparse row
keys, added nulls or numeric type coercion), rather than silently normalizing
legacy evidence. Reports must also be valid inputs to the existing ledger and
validation consumers, including validation's minimum ten chronological rows.

It runs `execution-ledger` and `validate-analytics` logic for both source and
compact artifact. All output must match except the explicitly expected container
path/content-identity fields: ledger's baseline path/hash, validation's baseline
and ledger path/hash, and sensitivity path/hash. Original market input provenance
is not stripped. Sensitivity validation includes the imported report itself.
The two normalized result fingerprints are retained in `manifest.legacy_import`,
along with exact legacy byte SHA-256, original filename and canonical report
SHA-256. This lineage is bound into the artifact identity. Existing non-imported
Phase-5 artifacts retain their identities and remain readable.

Source SHA-256 is checked again before atomic no-replace directory publication
(Linux/macOS). Existing files, empty directories and symlinks cannot be replaced,
even if created during staging. Import failure cleans its temporary files and
leaves the source and any existing artifact untouched. No automatic retry/resume
or overwrite is provided. A batch can retain successfully imported artifacts if
a later item fails; record per-item results before continuing.

Default detail retention is 30 days, unpinned. Use `--pin` only for selected
important evidence; `--retention-days` is explicit. Original reports remain
recoverable from the verified archive regardless of compact-detail TTL.

This migration adds convenient compact research detail back alongside package
A's archive; it is **not additional disk reclamation** relative to archive-only
storage. Measure the resulting size and do not count the already-removed JSON
a second time. [Workstation migration evidence, 2026-11-21](baseline-import-2026-11-21.md)
records 16 verified imports, 5.06 MiB added allocation, unchanged existing data
and identical production smoke results. Package C remains separately gated.
