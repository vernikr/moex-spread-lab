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

Strict typing is not enabled for the four complete packages; the first accounting
risk module is strict. Proof size/complexity exceptions and decomposition remain,
as do broader real-data parity checks and the final integrated acceptance gate.
P7.3 remains a separate work package.
