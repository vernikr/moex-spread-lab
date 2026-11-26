# Local data lifecycle and verified candidate archival

`packages/data` owns the local, git-ignored market-data footprint: the on-disk
layout, the read contract consumed by offline modules, incremental refresh
invariants, and read-only storage monitoring with explicit verified archival.
All market data is local-only under rights assumption `A-05`, must never be
committed and is never redistributed. Paths below are relative to a run `--root`.

## On-disk layout

| Path | Content | Posture |
|---|---|---|
| `data/w1/candles/`, `data/w1/candles-screen/` | normalized `iss-candles.v1` candle JSON; screened copies are the preferred runtime inputs | `active_input` |
| `data/w1/current-intake.json`, `data/w1/historical-contracts.json` | current contract snapshot and fallback terms; hashed into proof provenance | `active_input` |
| `data/w1/*-cache/`, `data/benchmark-cache/` | resumable intake caches; never runtime inputs | `rebuildable_cache` |
| `data/analytics/` | runtime settings, screen/run histories and user state (not market data) | `derived_runtime` |
| `data-lake/` | verified Parquet mirror of the JSON series with its authoritative `manifest.json` (`market-data-lake.v1`) | derived mirror |
| `data-lake/raw/` | retained raw ISS response bodies as exact `.json.zst` files | TTL retention |
| `data-lake/h1-seal.json`, `data-lake/h1-seal.enabled.json` | H1 seal control files: immutable copied records plus activation marker | control |
| caller-selected root outside `data/` and `data-lake/` (e.g. `../archives`) | verified `.tar.zst` archives, receipts, plans, apply journals, catalogs | `archives_and_evidence` |

The lake manifest is authoritative; a dataset record binds the dataset
identity, row range, source-page provenance and the exact legacy path plus
`legacy_sha256` it mirrors. Obsolete catalogs are ignored.

## Bar read contract

Offline readers consume `BarSeries` — normalized candle rows detached from
storage — through the `BarStore` protocol. `MarketDataStore` routes each legacy
JSON path:

1. a `*_1h.json` input inside an activated H1 seal reads as `sealed_parquet`;
2. otherwise a registered Parquet dataset whose record names this exact file
   and whose recorded `legacy_sha256` still matches the current file reads as
   `parquet`;
3. otherwise `legacy_json` reads the `iss-candles.v1` JSON directly.

Callers may project columns; each backend restores the same provenance
(`source_path`, `source_sha256`, price-proxy semantics), so the physical
backend never changes normalized output. H1 selection is screen-first: the
`candles-screen` copy wins while it exists, and a sealed selection keeps
reading the sealed source after its JSON disappears. A changed source file, an
unimported series or a missing lake transparently falls back to JSON; offline
readers never touch the network.

## Incremental refresh

`data-refresh` keeps each retained legacy JSON file and its verified Parquet
mirror in lockstep:

- a first dataset (or `--full-refresh`) requires `--from-date`; incremental
  runs derive the start from the stored watermark minus `--overlap-sessions`
  (default 5) and make zero network requests when the watermark already
  reaches `--till-date`;
- fetches are bounded (`--chunk-days`, `--workers`, `--retries`); chunks merge
  on the candle `begin` key, sort deterministically and publish atomically
  under an exclusive per-dataset writer lock, then re-run the
  legacy-versus-Parquet verification;
- fresh ISS bodies are retained under `data-lake/raw/` with
  request/response/receipt-time SHA-256 identity; unpinned entries expire
  after a default 30-day TTL, and `--pin-raw` or `data-pin-raw --entry-id …`
  protects evidence responses;
- `data-verify` checks every registered dataset and raw body; `data-compact`
  rewrites datasets as canonical Zstd Parquet files and refuses an active
  sealed table; `data-gc --apply` — after its default dry-run — removes only
  expired unpinned raw bodies and orphans; `data-import-candles` performs a
  one-off verified import of legacy JSON (`--replace` explicit).

## H1 sealed reads

H1 (60-minute) series may be sealed into immutable Parquet reads:

- `data-h1-stage --plan … --stage | --verify` builds and revalidates an
  isolated candidate lake (independent `candles/` and `candles-screen/`
  stores) from a deterministic plan of every `*_1h.json` in both source
  folders; staging never activates reads and never deletes sources;
- `data-h1-seal --activate --plan … --candidate …` copies the verified records
  into `data-lake/h1-seal.json` and binds the seal SHA-256 into
  `h1-seal.enabled.json`; `--verify` rechecks every sealed path;
  `--deactivate` restores JSON routing only after every original file and
  selection is restored;
- sealed reads verify table and source hashes (cached per
  inode/size/mtime/ctime signature) and fail closed on missing controls,
  changed JSON, new preferred screened copies, symlinks or unsealed paths —
  there is no fallback to JSON or an alternate source while a seal applies;
- sealing never deletes or rewrites sources; refresh or import into an active
  sealed table is refused until the seal is deactivated with restored JSON.

## Storage budget and archival

Monitoring is read-only; nothing deletes without an explicit, verified command.

- `storage-budget --root . [--archives-root …] [--max-allocated-bytes N]`
  reports logical/allocated bytes per disjoint scope (`data`, `data-lake`,
  external archives) across retention classes — active inputs, rebuildable
  caches, archive candidates, derived runtime and analysis results, raw and
  retained lake — plus TTL dry-runs. Exit code 2 signals an exceeded explicit
  budget; a budget alert triggers review, never deletion.
- `data-inventory [--fast | --hashes --output …]` builds the pinned SHA-256
  inventory manifest consumed by archival commands.
- `data-archive --manifest … --archive … [--apply]` archives only
  `rebuildable_cache` files into a verified, manifest-embedded tar outside
  `data/`; sources are removed only after full member verification.
- `data-archive-candidates` plans, archives and — in a separate maintenance
  window with writers stopped — applies reviewed archival of the selected
  non-runtime groups. Every step is plan-bound and revalidated against current
  file hashes, publishes no-clobber archives and receipts, records a durable
  apply journal, and requires an advisory lock. Restore into a new empty
  staging directory and compare restored sizes and SHA-256 against the
  embedded manifest before copying anything back.
- `archive-catalog --archive … --review-date … --output …` verifies each
  archive against its receipt and all member hashes and publishes a dated
  recovery catalog. Keep archives, receipts, plans, journals and catalogs
  together; a review date is not an expiry, and recovery archives for removed
  sources stay protected.
