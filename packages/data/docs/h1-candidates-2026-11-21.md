# Package C1 — full H1 candidate preparation

Date: 2026-11-21. Release: **0.6.0**. Implementation commit:
`0fb843b1bc0e61f6da15725bae5d0a07c67205d0`.
Status: **verified isolated candidates; not a production Parquet-only cutover**.

## Inventory and source selection

The workstation contained **120** runtime-named H1 JSON datasets in
`data/w1/candles`, **zero** in `data/w1/candles-screen`, and **448,565** total bars.
All 120 datasets were selected by the recorded screen-first source policy.

The deterministic plan includes exact relative paths, source namespace, SECID,
H1/60-minute identity, SHA-256, row counts, time bounds and source-page counts.
Source fingerprint:
`e40d53108e972d6fa001e33342ec38da5c927f3624a5ca5a9cc83e29eb8acbda`.
It rejects changed, removed or newly added sources rather than silently changing
the selection. Tests exercise differing copies of the same SECID in both folders
and confirm that the screened copy remains preferred.

The existing default data lake keys datasets by timeframe/SECID, which does not
represent multiple distinct source paths for the same instrument. C1 therefore
uses separate source-scoped candidate lakes, not replacement imports into the
production catalog. Exact-path identity and namespace routing still need to be
integrated into the eventual production reader.

## Workstation operation

Ran the CLI in `--dry-run`, `--stage` and `--verify` modes. The fresh candidate
root is `data-lake/candidates/h1-0fb843b/`, with a `candles/` sub-lake and a batch
`h1-stage.json` report. No screened sub-lake is needed for the empty group.

Every import and the completed batch passed JSON-vs-Parquet semantic validation:
normalized timestamp/OHLCV/value rows, row counts/time bounds, price-proxy
semantics, source-page provenance, metadata, file hashes and DuckDB identity.
A separate `data-verify --lake-root data-lake/candidates/h1-0fb843b/candles`
reported **120 verified datasets**. The default lake still has **4 verified
datasets**; these are retained pilot mirrors, not 4 additional unique H1 sources.

All **2,731 pre-existing `data/` files** and **12 pre-existing default-lake files**
retained their SHA-256. No JSON source was deleted, renamed, compressed in place
or replaced. The runtime manifest/catalog and source lookup rules were not
changed. Failed/interrupted candidate batches are explicitly non-activated;
there is no automatic resume or production discovery of a candidate batch.

## Measured size

| Metric | Bytes |
|---|---:|
| H1 JSON logical size | 357,395,916 |
| H1 JSON allocated size | 357,683,200 |
| Candidate bars + source Parquet logical size | 10,735,536 |
| Candidate bars + source Parquet allocated size | 11,038,720 |
| Entire candidate lake logical size, including catalog/manifests/report | 14,226,278 |
| Entire candidate lake allocated size | 14,794,752 |

This adds **14.11 MiB** of mirror allocation while keeping all JSON. It is **not
realized disk reclamation**. The size comparison is promising but does not meet
all removal gates: an archive, recoverable original inputs, sealed reader
integration and full cutover regression/rollback evidence are still required.
Do not count package A's savings again or assume these measurements prove cold
load/column-projection performance on every production workload.

## Regression and shadow-read evidence

- Full sandbox suite: **280 passed, 206 subtests passed** (55.99 s).
- Full workstation suite: **280 passed, 206 subtests passed** (151.27 s; overlapped
  candidate staging, not a performance comparison).
- Existing fixed smoke calculations were run in a separate process with a scoped
  `MarketDataStore.read` override routing H1 exact source paths to their verified
  candidate namespace. Non-H1 reads retained the original reader. The override
  was not installed into the running service and no settings were written.
- Shadow execution used **all 120 candidate source paths** through the catalogs
  and fixed cases. Both daily/hourly catalog hashes and all five stable result
  fingerprints/run identities exactly matched the pre-archive baseline:
  daily pair proof `11cac6188af03fc5`, pair-screen result `f8c6ec9f95e4f077`,
  funding proof `614221cf66cd401b`, hourly pair proof `13cbd4068aa25e41`, and hourly
  calendar proof `1c95501d44db577b`. The
  [package-A report](data-archive-2026-11-21.md) defines selections, comparison
  fields and the pair-screen series-count caveat.
- This is bounded shadow-equivalence evidence, **not** exhaustive rank/score,
  plateau/search, rejection, browser or cold-load regression coverage for a
  sealed production reader. No such reader is delivered in C1.

Local evidence, outside Git, remains in the sibling archive/evidence directory:
`h1-plan-0fb843b.json`, `h1-stage-result-0fb843b.json`, `stage-h1-0fb843b.py`,
`h1-shadow-smoke-0fb843b.py`, `smoke-shadow-h1-0fb843b.json`,
`shadow-usage-h1-0fb843b.json` and `tests-h1-0fb843b.txt`.
Candidate-local `h1-stage.json` contains per-source verification evidence.

## Next: C2 gates

1. Implement production exact-path, source-scoped sealed Parquet selection;
   preserve `candles-screen` priority even after its JSON is archived.
2. Remove existence-based reader assumptions only with explicit verified sealing;
   missing/corrupt preferred data must fail closed rather than select another copy.
3. Exercise all production readers and fixed search/rank/score/plateau/rejection,
   calendar/funding and UI cases, preserving run identities and measuring cold load.
4. Create and fully restore-verify the active-H1 archive, then measure net storage
   including archive and candidate catalog costs against the agreed threshold.
5. Only after those gates consider supervised active JSON removal. C1 has
   `runtime_activated=false` and `source_deletion_allowed=false` by design.
