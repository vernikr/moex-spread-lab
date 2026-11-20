# D — steady-state storage policy, 2026-11-21

Release 0.9.0, implementation `174069e`. Added read-only `storage-budget` and
verified `archive-catalog`; operational cadence and explicit apply rules are in
`data-lifecycle.md`. No scheduled deletion or compaction job was installed. No
research input, archive, retention pin or TTL was changed in this package.

## Workstation validation

- Linux: 312 tests + 206 subtests passed, 68.07 s.
- Mac: 312 tests + 206 subtests passed, 109.71 s.
- Both A and C archives passed archive SHA, embedded-manifest and every-member
  streaming hash checks. Catalog includes full manifests, original release
  commits, recovery instructions and review date **2026-12-21**, not expiry.
- H1 seal verified 120 datasets, zero retained JSON; default lake verified 4.
- Five daily/funding/screen/hourly/calendar results and catalogs exactly match
  the prior smoke baseline. No new cold-disk performance claim is made.
- Active H1 has 240 Parquet files (bars and provenance), each with one row group;
  total 449,524 rows across both table types. Compaction was not performed.

## Allocated budget snapshot

| Class | Files | Allocated bytes |
|---|---:|---:|
| Active JSON/runtime inputs | 2,530 | 52,711,424 |
| Archive candidate (classification only) | 1 | 36,864 |
| Derived archive candidates (classification only) | 3 | 5,398,528 |
| Derived runtime | 5 | 2,932,736 |
| Retained lake (including active H1) | 259 | 18,239,488 |
| Compact run artifacts | 64 | 5,308,416 |
| Unclassified | 8 | 1,585,152 |
| Archives and evidence | 60 | 30,322,688 |
| **Total** | **2,930** | **116,535,296 (111.14 MiB)** |

Default raw footprint and expired/orphan counts were zero. Run TTL plan retained
16 artifacts, expired zero and planned zero detail deletions. Classification is
not deletion permission: e.g. inventory/evidence JSON may be labeled derived
archive candidate without being eligible for the dedicated candidate archiver.
Sealed H1 is counted under retained lake, not omitted from the total.

This is a live point-in-time sum of file allocation, not unique APFS physical
blocks. Hardlinks/clones can be double-counted. Snapshot excludes its own output
and subsequent logs/evidence; later totals will differ slightly. No hard byte
budget was chosen on the user's behalf. `--max-allocated-bytes` is available for
operator-selected alert thresholds; exceeding it never deletes anything.

## Local evidence (not committed)

In the existing workstation `moex-spread-lab-archives` directory:

- `archive-catalog-d-174069e.json`
- `storage-budget-d-174069e.json`
- `row-groups-d-174069e.json`
- `tests-d-174069e.txt`
- `smoke-d-174069e.json`

The catalog covers `candidates-71a5525.tar.zst` (1,169 original files) and
`active-h1-594c183.tar.zst` (120 original files). It is a verified recovery index,
not a second backup. An independent copy on another physical medium remains an
operator task requiring a specified destination. Never delete these archives,
receipts or the active sealed candidate to satisfy a budget alert.
