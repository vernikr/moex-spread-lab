# Stage-9 research gate decision

Date: 2026-11-03

## Decision

**Do not promote the current CNYRUBF/CRZ6 baseline configuration from offline analytical research to an executable or funded pilot.** Keep the pair conditionally admitted for local research only. The current result is `inconclusive_no_pass`, with the executable/pilot gate **blocked**.

This is an implementation gate conclusion based on the delivered artifacts. It is not a user authorization, a broker decision, an investment recommendation or a permanent economic rejection of the pair. It rejects the current draft rule and its promotion path, not every possible strategy, horizon, execution mode or future contract-evidence package for CNYRUBF/CRZ6.

## Scope of the decision

Three questions must remain separate:

| Question | Current conclusion |
|---|---|
| Did this fixed mean-reversion baseline beat matched LQDT in the analytical run? | No; it ended at `X = −RUB 7,572.30` before unknown real costs. |
| Can this baseline be promoted to an executable/funded pilot now? | No; execution, cost, dated-specification and account gates are blocked. |
| Is CNYRUBF/CRZ6 permanently unsuitable for every future strategy? | Not established. The pair remains conditionally admitted for offline research. |

The illustrative cost branches mainly show that this particular high-turnover baseline is fragile. They do not establish that every slower, carry-oriented, event-driven or otherwise differently designed strategy on the pair would fail.

## Evidence basis

The frozen daily baseline used 344 synchronized observations, one contract per leg, a one-bar delay and no parameter tuning. Its sealed confirmation ended at `2026-10-27` with:

- strategy value proxy: RUB 29,433;
- matched LQDT value: RUB 37,005.30;
- final `X = V_proxy − B`: −RUB 7,572.30;
- gross futures P&L: −RUB 567 across 11 virtual round trips.

The W4/W5 ledger recorded 22 entry/exit attempts but could not treat any as a historical fill. The W7 report has chronological train, validation and sealed confirmation segments, delay/cost sensitivity and a sign-flip diagnostic. The sign-flip branch ended at −RUB 6,438.30, better than the observed branch; this does not provide statistical evidence and reinforces the need for caution.

## Blocking conditions

The promotion gate remains closed because:

1. the project deliberately has no historical order-book or actual-fill data; the candle-close proxy is accepted for offline medium-term research but cannot support a claim about real fills;
2. account-applicable exchange, clearing, slippage, funding and exit costs are not evidenced for the historical window; the user's VTB broker commission is now evidenced as zero through 2026-12-31;
3. current contract parameters are a fallback, not dated historical specifications;
4. the ledger is a candle-close proxy projection, not an account valuation based on actual orders;
5. account collateral, individual coefficients, margin-offset and LQDT attribution reconciliation remain unresolved; EBS and КПУР are now user-confirmed and documented in the [VTB account risk profile](../../packages/accounting/docs/vtb-account-risk-profile.md).

The separate [contract-history audit](contract-history-audit.md) adds an important scope distinction: the draft price-only baseline is not a complete historical carry test. The pair remains eligible for a separately specified funding-aware hypothesis, subject to its own evidence and validation gates.

Under the pilot policy, missing safety-critical evidence cannot be converted to zero cost, safe collateral, guaranteed fills or permission to fund/trade. The present result therefore cannot satisfy the W4/W5/W7 gates for an executable conclusion.

## Allowed continuation

Offline work may continue without broker access, funding, deployment or order submission:

- preserve the baseline, ledger and W7 report as the current reference run;
- keep the price-only baseline and any future funding-aware carry hypothesis as separate experiments;
- treat the current H2 funding decomposition as gross evidence only, not as a tested strategy result;
- treat H2-S1 as a separate frozen proxy backtest with `final X = −RUB 7,610.59`; it is not promoted and is not combined with the H1 ledger;
- treat H3-F1 as a separate frozen funding-only proxy backtest with `final X = −RUB 7,694.91`; it is not promoted and is not combined with H1 or H2-S1;
- use the [point-in-time evidence audit](point-in-time-evidence-audit.md) to segment CNYRUBF funding regimes and keep perpetual assignment handling explicit;
- audit dated contract history and expiry/roll specifications separately before any pre-2020 claim;
- preserve the no-order-book boundary under A-08 and model manual reaction delay plus cost sensitivity;
- use the user-provided VTB tariff as the current broker-cost input through 2026-12-31, while keeping exchange fees and unverified account controls separate;
- rerun the same frozen report after new evidence, without resetting `X`, `H`, `D` or funding history.

Parameter optimization of this baseline, M1 promotion of this high-turnover rule, scale-in design and live connectivity are deferred until the evidence blockers are closed. A future strategy hypothesis may be evaluated separately, with its own frozen specification, turnover, cost sensitivity and validation record. It must beat the matched LQDT path after verified costs and delayed execution while passing the finite-capital and risk gates; failure of one hypothesis must not be reported as rejection of the pair itself.

## Reproducible artifacts

- [Analytical baseline](../../packages/strategies/docs/analytical-baseline.md)
- [Execution ledger](../../packages/accounting/docs/execution-ledger.md)
- [W7 validation](../../packages/accounting/docs/w7-validation.md)
- [Pilot policy](../risk/pilot-policy.md)
- [Account and risk blockers](../blockers/b01-account-and-risk.md)
- [VTB EBS/KПУР account-risk screen](../../packages/accounting/docs/vtb-account-risk-check.md)
- [Flagship futures-pair screen](../../packages/strategies/docs/pair-screen.md)
- [Funding-aware flagship-pair screen](../../packages/strategies/docs/funding-screen.md)
- [Workflow and research blockers](../blockers/b02-research-and-workflow.md)
