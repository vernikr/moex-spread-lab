# Decision register

Updated 2026-10-27 after the user's W1 data-use clarification. User answers override proposed interpretations; project assumptions and delegated defaults remain distinct from verified external facts.

| ID | Confirmed user decision | Boundary / unresolved part |
|---|---|---|
| A-01 | Initial test operates on the same account as LQDT. | No legal/operational ring-fence inferred; collateral and broker recourse unverified. |
| A-02 | Exceeding the planned RUB 30,000 requires explicit user approval. | The delegated default permits proposals for at most 15,000 additional gross funding over the whole pilot, for existing-position liquidity/de-risking only. Each use still needs explicit approval. Emergency availability and delegated ceiling selection are not approval to transfer money. |
| A-03 | Use one realistic virtual portfolio. Attempt all eligible signals without discretionary omission; record constraints and reasons for nonexecution. | No fictional fills when price, capital, tradability or validity prevents execution. No second unconstrained portfolio requested. |
| A-04 | Acceptable final **underperformance versus LQDT** over several months: **RUB 10,000–15,000**. Emergency top-ups are possible. | The user resolved the numeric wording and then the benchmark-relative loss basis. Delegated risk defaults below specify action rules; per-idea/aggregate stress calibration and tax basis remain unverified. |
| A-05 | For this private, single-user pet project, treat freely accessible public market data as available for the project's local research, storage and calculations; do not block the research pipeline on unresolved ISS-use terms. If better historical contract evidence is unavailable, use the current contract parameters as an explicitly labelled fallback assumption. | This is a project operating assumption, not a legal opinion or proof of MOEX permission. It does not create redistribution, public-service, broker, live-trading or unattended-runtime authorization. Historical executable bid/ask remains a separate data limitation. |
| A-06 | For medium-term analytical research, treat the close of an ISS candle (1m/10m/1h/1d) as the project's working price proxy for an available market price at that interval. Use the proxy with delay and cost sensitivity; do not present it as a reconstructed historical bid/ask or guaranteed fill. | This is a modelling assumption for exploratory analysis. It does not resolve intrabar order sequence, two-leg synchronization, queue position, partial fills or execution price uncertainty. |
| A-07 | Use the inspected reference methodology for a two-layer LQDT benchmark: observed `VTBM → LQDT` and an explicitly labelled extended proxy `CBR_RUONIA → FXMM → RUSFAR → VTBM → LQDT`, stitched by returns after VTBM split correction. | Proxy periods are not actual LQDT prices and cannot replace the observed benchmark for a clean account-level conclusion. Missing proxy source rows remain missing; no rate or price is invented. |

## Delegated operational defaults (not empirical findings)

The user asked the assistant to choose reasonable interim-drawdown and maximum-top-up values and to proceed to stage 2. The assistant selected the following research/pilot defaults; full definitions and consequences are in [the versioned policy](risk/pilot-policy.md).

| Control | Selected default | Status |
|---|---|---|
| Excess-curve peak drawdown | 5,000 warning; 10,000 action trigger | Assistant-selected under delegation; needs stress/execution validation. |
| Cumulative LQDT shortfall | Act/exit at 10,000; outer boundary 15,000 | Uses the user’s range to leave an action buffer; not a guaranteed loss cap. |
| Additional funding | 15,000 lifetime cumulative gross; no replenishment by withdrawals | Per-event user approval remains mandatory; no new exposure financed by rescue money. |
| Gross contributed envelope | 45,000 including original 30,000 | Internal funding ceiling, not a notional/maximum-loss/broker-recourse limit. |
| Review convention | 90 calendar days from activation | Provisional planning convention; no forced time exit or resets. |
| Numeric reporting basis | Before investor-specific taxes, after trading/funding costs | Provisional implementation convention; show tax sensitivity pending R05 and actual tax facts. |

User requested advancement to stage 2. Research reports, strategy approval, live activation and actual funding remain separate gates.

Repository created private as `vernikr/moex-spread-lab`. Name chosen as a working label, not a user-specified product name.

No user approval of a strategy, stack, legal interpretation, automated trading permission or VPS deployment is implied by these decisions.
