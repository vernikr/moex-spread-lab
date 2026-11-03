# Stage-9 research gate decision

Date: 2026-11-03

## Decision

**Do not promote CNYRUBF/CRZ6 from offline analytical research to an executable or funded pilot.** Keep the pair conditionally admitted for local research only. The current result is `inconclusive_no_pass`, with the executable/pilot gate **blocked**.

This is an implementation gate conclusion based on the delivered artifacts. It is not a user authorization, a broker decision, an investment recommendation or a permanent economic rejection of every future version of the pair.

## Evidence basis

The frozen daily baseline used 344 synchronized observations, one contract per leg, a one-bar delay and no parameter tuning. Its sealed confirmation ended at `2026-10-27` with:

- strategy value proxy: RUB 29,433;
- matched LQDT value: RUB 37,005.30;
- final `X = V_proxy − B`: −RUB 7,572.30;
- gross futures P&L: −RUB 567 across 11 virtual round trips.

The W4/W5 ledger recorded 22 entry/exit attempts but could not treat any as a historical fill. The W7 report has chronological train, validation and sealed confirmation segments, delay/cost sensitivity and a sign-flip diagnostic. The sign-flip branch ended at −RUB 6,438.30, better than the observed branch; this does not provide statistical evidence and reinforces the need for caution.

## Blocking conditions

The promotion gate remains closed because:

1. historical executable bid/ask, depth, queue and fills are unavailable;
2. account-applicable exchange, clearing, broker, slippage, funding and exit costs are not evidenced for the historical window;
3. current contract parameters are a fallback, not dated historical specifications;
4. the ledger is a candle-close proxy projection, not an executable account valuation;
5. account collateral, liquidation, KPUR, margin-offset and LQDT attribution reconciliation remain unresolved.

Under the pilot policy, missing safety-critical evidence cannot be converted to zero cost, safe collateral, guaranteed fills or permission to fund/trade. The present result therefore cannot satisfy the W4/W5/W7 gates for an executable conclusion.

## Allowed continuation

Offline work may continue without broker access, funding, deployment or order submission:

- preserve the baseline, ledger and W7 report as the current reference run;
- audit dated contract history and expiry/roll specifications separately before any pre-2020 claim;
- collect and validate applicable tariff, collateral and execution evidence through the research prompts;
- add quote/fill fixtures only when they are clearly labelled as observed, simulated or unresolved;
- rerun the same frozen report after new evidence, without resetting `X`, `H`, `D` or funding history.

Parameter optimization, M1 promotion, scale-in design and live connectivity are deferred until the evidence blockers are closed. A future run must beat the matched LQDT path after verified costs and delayed execution while passing the finite-capital and risk gates; otherwise the same no-promotion decision remains valid.

## Reproducible artifacts

- [Analytical baseline](analytical-baseline.md)
- [Execution ledger](execution-ledger.md)
- [W7 validation](w7-validation.md)
- [Pilot policy](../risk/pilot-policy.md)
- [Account and risk blockers](../blockers/b01-account-and-risk.md)
- [Workflow and research blockers](../blockers/b02-research-and-workflow.md)
