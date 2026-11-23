# P7.2 incremental evidence

Status: **in progress**, not an acceptance record for the complete work package.

## Cohort preparation on workstation — 2026-11-23

The `cohort_optimize` suite compared the live server at `25f0037` with an
isolated candidate based on the same revision, replacing only `proof_batch.py`
with its `c350867` version (the unused new module remained present). The
workstation supplied its own market data; the candidate data mode was APFS
clone. The suite used ten Si calendar targets, 1d timeframe, seed 41, 80 trials,
one measured call, no warmup, and six worker processes. The baseline was the
live server and potentially warm; the candidate was a new server.

After the benchmark's provenance-path normalization, the **full** result digest
was identical on both sides:
`611ec85bdc1c3e91d8dacc64e4b105d6a84095c39cff13b89b98775e60386907`.
Both returned `run_id` `381ff296b49b5c52` and `trial_count` 80; the benchmark
reported `exact_match: true`. This supports parity for this workload only, not
all cohorts, cancellation paths, or the subsequent accounting/service changes.
The observed elapsed times (2.27 s live, 4.90 s candidate) are not a valid
performance comparison because cache and server startup states differed.

The synthetic mixed-cohort golden test separately locks the complete normalized
payload and run ID; existing tests cover pool and cancellation behavior. Neither
claim replaces a complete P7.2 acceptance run.

## Remaining

Strict typing is enabled for all accounting source; the other three complete packages remain. Proof size/complexity exceptions and decomposition remain,
as do broader real-data parity checks and the final integrated acceptance gate.
P7.3 remains a separate work package.

## Incremental typing and workstation probes — 2026-11-23

Strict enforcement was extended to selected accounting, data, proof and
strategies files, without changing the typing baseline or disabling rules.
A diagnostic-only whole-package strict run at `c08d4f4` reported 4,152 errors:
proof 3,108, data 672, strategies 308 and accounting 64. This is not a passing
gate. The earlier 3,995-error measurement predates new proof preparation code;
the totals are not a like-for-like measure of strict progress.

A candidate at `c0e42a9` included the changes to `accounting/costs.py`,
`accounting/sizing_report.py`, and proof's `funding_sizing_payload.py`,
`mean_reversion_spec.py`, `screener_math.py`, `screener_search.py` and
`worker_weight.py` from `c08d4f4`. No other changes in the current series were
included. Against the live server at `c0e42a9`, the workstation benchmark
reported `exact_match: true` for all four `single_proof_1d_1h` cases (CNY and
gold, 1d and 1h) and the `cohort_optimize` case. Full normalized payload hashes
and run IDs matched; the single-proof reconciliations were zero. The cohort
used six workers, seed 41 and 80 trials, with run ID `381ff296b49b5c52` and
normalized SHA-256 `611ec85bdc1c3e91d8dacc64e4b105d6a84095c39cff13b89b98775e60386907`.
Each case used one call with no warmup; live and new candidate cache states
were different, so timings are not a performance comparison. These probes do
not establish parity for untested changes, strategies or datasets.

## Accounting strict and carry settlement — 2026-11-23

At `61dacf4`, accounting source is enabled in strict mode as a whole. PyArrow
19.4 stubs were added as a development dependency; a narrow cast documents a
stub limitation for valid string Parquet filters, and the artifact adapter
narrows partially unknown third-party callable signatures. No typing baseline
or checker rule was relaxed. Diagnostic-only strict over all four target
packages reports 4,030 errors: proof 3,146, data 576, strategies 308,
accounting 0. This measurement uses the newly installed stubs and changed proof
code, so it is not directly comparable with the earlier 4,152 count.

Workstation A/B against live `d231a44` used an isolated candidate containing
only the six sizing/core files changed in `cf3cee7`. All four single-proof
1d/1h workloads and the Si calendar cohort matched in full normalized payload
hash and run ID, with zero ledger reconciliation for the single proofs. The
cohort again used six workers, 80 trials, seed 41 and run ID
`381ff296b49b5c52`. A second candidate added only the carry accounting file
from `61dacf4` to those six files: a CNYRUBF/CRZ6 `funding_only`, 1d,
`max_affordable` run matched (`run_id` `ca6cc884d090f3c4`, normalized SHA-256
`c2447b1f09f53de8f4b5c8018ed5e495d76aa11a46e2bc324ea1f195289a69c0`,
ledger reconciliation zero). Each test used one call and no warmup. The
candidate did not include the artifact or PyArrow-stub changes; these checks
are workload-specific and do not establish general equivalence or latency.

## Screener decomposition and calendar typing — 2026-11-23

Screener search was split into grid/neighbor construction, trial evaluation and
plateau selection while keeping the facade and its injectable collaborators.
The size and `optimize` complexity exceptions for `screener_search.py` were
removed. A synthetic two-pair validation case was run on both `b667fb7` and
the refactor: the complete canonical result SHA-256 was
`8a6d812c93c0ed288790129526c6acf50f716f641776948815e5046dcb2ebf27`,
with `run_id` `f0b69e50b3ea3ce1` and 27 trials. This is now a regression
test; it does not establish parity for every legacy screener scenario.

A workstation A/B against `b667fb7` included the 12 changed strategy source
files from `dae690e`, `aa95079` and `e5cba8b`, not the screener refactor.
`calendar_proof` matched the full normalized payload and run ID for both
calendar z-score (`06fb9bd340da3ccb`) and RUONIA convergence
(`1af0fb578e35154a`); the ledger reconciliations were zero. The Si cohort also
matched (`381ff296b49b5c52`, 80 trials, six workers). One call was made per
workload without warmup; differing live/candidate cache states make this
unsuitable for performance conclusions. The pure-Python/C equivalence suite
passed; clang was unavailable in the sandbox, so the sanitizer was not run.

A diagnostic-only whole-package strict measurement at `e5cba8b` reported
3,955 errors: proof 3,111, data 576, strategies 268, accounting 0. The strict
configuration used for this measurement was temporary; the normal gate still
includes full accounting and selected modules of the other packages. The target
remains all four packages, with no new baseline or rule suppression.
