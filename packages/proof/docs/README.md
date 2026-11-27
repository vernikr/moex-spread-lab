# `msl-proof` (Layer 3) — durable module guides

These are the durable references owned by `packages/proof`. They describe
contracts and invariants, not live work: current status and temporary reports live
only in GitHub Issues and Project #2.

| Guide | Owns |
|---|---|
| [execution-proof.md](execution-proof.md) | The `/`, `/proof` workbench contract: cohort coordinator, common-strategy search, lamps and scores, automatic per-entry sizing, global capital and seed, research screens, price fills, window/period table, funding-evidence policy, runtime performance contracts and every dated regression rule. |
| [profit-audit.md](profit-audit.md) | A two-line pointer to the archived W1–W7 audit and S3 reference reproduction (kept until #229 updates the remaining inbound links); the durable rules live in `docs/decisions.md` (A-11/A-12/A-13). |

Cross-cutting governance stays in the repository root: Stage-9 invariants and
`run_id`/hash rules in `docs/stages/*`, approved boundaries in
`docs/decisions.md`, risk limits in `docs/risk/pilot-policy.md`, and CI/agent
workflow in `docs/development/`.

## Boundaries this package owns

- Proof orchestrates replay, cohort search and audit under the accounting
  contracts of `msl-accounting`; it never widens capital, funding or trading
  authority, and it never reconstructs executable fills.
- `run_id` parity and `packages/proof/tests/test_perf_equivalence.py` are merge
  gates: any change to payload shape, hashing input or fast path must preserve
  them, and a change that cannot is a contract decision, not a refactor.
- Relaxing a research screen in the workbench changes admissibility for the search
  only. It is not funding, not shared capital and not a claim of executable
  returns.

## Verification

```sh
uv run pytest packages/proof/tests -q
uv run pytest packages/proof/tests/test_perf_equivalence.py -q      # parity gate
uv run python scripts/verify.py --profile full                     # before push
```
