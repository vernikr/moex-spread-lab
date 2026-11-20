# Package A — verified archive application

Date: 2026-11-21. Status: completed on the workstation following the user's
continuation approval. Runtime release: 0.4.0, code commit
`71a55254f009e6754229ee0a0de5df3d8f9e2c22`. This is operational evidence, not
approval of trading or a performance benchmark.

## Scope and integrity

The reviewed CLI plan selected 1,169 files / 680,006,983 logical bytes. Compared
with the older 1,170-file ad-hoc plan, only `analytics/data-inventory.json`
(2,433,049 bytes) was excluded as control-plane evidence; the remaining paths,
sizes and hashes matched. Source fingerprint:
`441bcf8086eb0e53e23b0250ccdc6daba1ae2fbbd4ce9ceab4e0a81526139c45`.

Archive: `candidates-71a5525.tar.zst`, under the workstation's sibling
`moex-spread-lab-archives` directory (outside the checkout). Archive SHA-256:
`c9102a591cf0e345a0508ce4cf9265634361071c1b14892fd8abdc753230e1b6`.

1. Saved a fresh, hashed inventory of all 3,836 data files.
2. Ran `archive-only`: Zstd level 19, embedded manifest and streamed all-member
   verification, no-clobber publication, verification receipt.
3. Extracted the complete archive into a new temporary directory. All 1,169
   restored files matched manifest paths, sizes and SHA-256; embedded manifest
   matched the receipt. Removed only that temporary restore copy afterwards.
4. Unloaded the existing supervised screener for a maintenance window, confirmed
   no other project CLI processes and no listener on its port. Verified all
   nonselected files before applying.
5. Ran explicit `apply`; its report recorded exactly 1,169 deleted paths. Verified
   every selected path was absent and all other 2,667 files retained their sizes
   and SHA-256. Saved an after-inventory. Active H1, screen-priority inputs,
   benchmark, funding/calendar caches, runtime settings and unclassified files
   were retained.
6. Recomputed the fixed smoke cases below, compared with pre-apply results, and
   reloaded the supervised service. No settings were written by the smoke runner;
   daily/hourly overrides were scoped to its process memory.

## Measured storage

File allocation uses `st_blocks * 512`, not JSON length. Measurements exclude
filesystem directory metadata, unrelated application writes and dependency caches.

| Metric | Bytes |
|---|---:|
| `data/` allocated before | 1,103,929,344 |
| `data/` allocated after | 420,347,904 |
| Removed source allocation | 683,581,440 |
| Archive logical size | 14,013,095 |
| Archive allocated size | 14,725,120 |
| Entire archive/evidence directory allocated after tests | 18,415,616 |
| Conservative net allocated reduction | **665,165,824 (634.35 MiB)** |

The conservative net subtracts the **entire** archive/evidence directory,
including the plan that already existed before this operation. The gain is
compression plus removal of verified duplicates, not moving files between
folders. Same-SSD archival is not an independent backup against disk loss.

## Regression evidence

Production-input smoke comparisons preserved run identities and SHA-256 of the
available stable result fields: `run_id`, `summary`, `window`, `selection`,
`provenance`, `series`. Full daily/hourly catalog JSON hashes also matched.

| Check | Selection | Observations | Unchanged run ID |
|---|---|---:|---|
| Daily pair proof | CRZ6/CRH7, mean reversion | 155 | `11cac6188af03fc5` |
| Pair-screen result | CRZ6/CRH7, mean reversion | n/a | `f8c6ec9f95e4f077` |
| Funding proof | GAZPF/GZH5, funding only | 121 | `614221cf66cd401b` |
| Hourly pair proof | USDRUBF/SiZ2, mean reversion | 2,070 | `13cbd4068aa25e41` |
| Hourly calendar proof | CRU6/CRZ6, z-score mean reversion | 1,183 | `1c95501d44db577b` |

Initial CNYRUBF/CRZ6 strict proof selection was rejected by the existing liquidity
gate **before** archival. Smoke selections were therefore taken from eligible
catalog entries, not by relaxing the gate. Catalog equality includes exclusions.
Pair-screen output has no `series` field; its script's zero series count is not a
zero-observation research claim.

- Full suite in the sandbox: **254 passed, 206 subtests passed** (56.84 s).
- Full suite on the workstation after apply: **254 passed, 206 subtests passed**
  (98.25 s). Tests use isolated fixtures; production smoke is separate above.
- Workstation `data-verify`: **4 datasets verified**.
- Reloaded service: `/` and `/proof` returned HTTP 200. This is an HTTP smoke,
  not a claim of visual browser regression coverage.
- No claim of runtime speed improvement; this operation targets disk allocation.

## Evidence, restore and next steps

Local evidence stays beside the archive, not in Git: original plan, verification
receipt, exact apply journal, before/after hashed inventories, full-restore
report, before/after smoke fingerprints, smoke/operation scripts and workstation
test log. `README-71a5525.md` records restore instructions, release commit,
archive hash and next review date **2026-12-21** (not automatic expiry).

Restore only into a new empty staging directory, validate the archive against
its receipt and recorded hash, then check every extracted file against the
embedded manifest before selectively restoring original paths. Never overwrite
changed working files automatically. Preserve the archive and evidence.

Packages B (legacy baseline import) and C (verified H1 Parquet-only cutover)
remain unimplemented. Package B must read needed legacy inputs from a verified
staged restore of this archive; do not rehydrate the whole archive permanently
into `data/`. No active H1 JSON deletion is authorized by this completed package.
