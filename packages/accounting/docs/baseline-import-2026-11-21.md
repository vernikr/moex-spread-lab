# Package B — legacy baseline migration evidence

Date: 2026-11-21. Status: implemented and applied on the workstation.
Release: **0.5.0**; implementation commit:
`357e59c8d97f54c8281d78b200238a9bad1b3875`.

## Sources and publication

The package-A archive was rehashed and all members were verified against its
embedded manifest and receipt before extracting analytical reports. Its SHA-256
remains `c9102a591cf0e345a0508ce4cf9265634361071c1b14892fd8abdc753230e1b6`.

Exactly **16** archived `analytical-baseline.v1` reports were imported into
`data/analytics/runs/legacy-<original-stem>/`. Each directory contains
`summary.json`, `daily.parquet`, `trades.parquet`, and `manifest.json`. Import
order was M1 diagnostics, 10m sensitivity reports, then the remaining reports.

The 13 other analytical reports (calendar study, ledger, carry/funding reports,
validation, audits and reference reports) remain complete and losslessly
archived. No generic conversion was attempted for their schemas. Source JSON
was staged outside the checkout; every staged file matched the archive's size
and SHA-256. Staging was removed after verified import; no legacy JSON was
permanently restored into `data/`.

For every imported report:

- Canonical JSON of the complete rehydrated artifact matched the source,
  including all daily/trade rows, summary, final X, drawdown, parameters and
  original input provenance; no tolerance-based financial comparison was used.
- `execution-ledger` and `validate-analytics` consumer outputs matched after
  removing only the documented container path/identity fields. Both consumer
  fingerprints, legacy byte hash and canonical report hash are integrity-bound
  in `manifest.legacy_import`.
- Publication used atomic no-replace rename on macOS. The original verified
  archive remains unchanged and is the recovery source for original JSON.

## Size and retention

| Group | Reports | Legacy logical bytes | Compact logical bytes | Daily rows | Trades |
|---|---:|---:|---:|---:|---:|
| M1 diagnostic | 2 | 111,315,401 | 2,586,639 | 214,728 | 13,670 |
| 10m sensitivity | 5 | 49,859,116 | 1,944,488 | 95,300 | 5,550 |
| Daily baseline/cost | 4 | 720,661 | 129,336 | 1,376 | 44 |
| H1 sensitivity | 5 | 11,438,057 | 572,152 | 21,930 | 1,140 |
| **Total** | **16** | **173,333,235** | **5,232,615** | **333,334** | **20,404** |

The logical-size ratio is **33.13x**. Allocated size of the 64 compact files is
**5,308,416 bytes (5.06 MiB)**, including summaries and manifests. This is an
**addition** to package A's archive-only state, not another disk-space saving:
the large originals had already been removed by package A. The benefit here is
restored research access through compact verified artifacts, without bringing
back 173 MB of pretty JSON. Do not double-count package A's reclamation.

Only `legacy-cnyrubf-crz6-1d-baseline` is pinned as the canonical baseline evidence.
The other 15 runs retain the default unpinned 30-day detail TTL. The archive has
no automatic deletion policy. A GC dry-run reported 16 retained artifacts,
zero expired artifacts and no planned deletions. No GC apply was performed.

## Validation and boundaries

- **2,667** pre-existing data files retained identical bytes/SHA-256.
- Full test suite in the sandbox: **268 passed, 206 subtests passed** (58.31 s).
- Full test suite on macOS: **268 passed, 206 subtests passed** (102.19 s).
  Tests cover lossless roundtrip, lineage tampering, malformed/duplicate-key JSON,
  NaN, unsupported schemas, sparse rows/numeric coercion, invalid consumer input,
  source mutation, consumer mismatch, expected hash, pin/CLI behavior, existing
  files/directories/symlinks and concurrent destination creation.
- The package-A production-input smoke script was rerun. Both daily/hourly
  catalogs and all five stable result fingerprints/run identities matched its
  pre-archive baseline: pair-screen result, daily pair proof, hourly pair proof,
  funding proof and hourly native calendar proof. See the
  [package-A evidence](../../data/docs/data-archive-2026-11-21.md) for selections and identities.
- `data-verify` reported **4 verified datasets**.
- No active H1 candle files, screened-priority inputs, benchmark lineage or
  funding/calendar caches were changed. H1 **analytical reports** in the table
  are not active H1 candle datasets.
- These are equivalence and storage measurements, not runtime speed or
  profitability claims. No broker interaction or live-trading authority.

Local evidence beside the archive: `baseline-import-357e59c.json` contains every
source member, hash, artifact identity, consumer fingerprints, retention and
size; `migrate-baselines-357e59c.py` records the supervised batch procedure;
`tests-import-357e59c.txt` and `smoke-after-import-357e59c.json` record validation.
No generated market data or research detail is committed to Git.

## Next gate

Package C remains separate: enumerate exact H1 source selections, preserve
`candles-screen` priority, import/verify every dataset, implement a sealed
Parquet-only reader and prove regression/rollback behavior before considering
any active JSON removal. Completing package B does not authorize skipping that
gate. The storage plan's steady-state retention/catalog work remains ongoing.
