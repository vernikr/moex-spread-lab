# Decision register

Updated 2026-10-07 after the user’s second instruction. User answers override proposed interpretations; delegated defaults remain distinct from direct user choices and from verified facts.

| ID | Confirmed user decision | Boundary / unresolved part |
|---|---|---|
| A-01 | Initial test operates on the same account as LQDT. | No legal/operational ring-fence inferred; collateral and broker recourse unverified. |
| A-02 | Exceeding the planned RUB 30,000 requires explicit user approval. | The delegated default permits proposals for at most 15,000 additional gross funding over the whole pilot, for existing-position liquidity/de-risking only. Each use still needs explicit approval. Emergency availability and delegated ceiling selection are not approval to transfer money. |
| A-03 | Use one realistic virtual portfolio. Attempt all eligible signals without discretionary omission; record constraints and reasons for nonexecution. | No fictional fills when price, capital, tradability or validity prevents execution. No second unconstrained portfolio requested. |
| A-04 | Acceptable final **underperformance versus LQDT** over several months: **RUB 10,000–15,000**. Emergency top-ups are possible. | The user resolved the numeric wording and then the benchmark-relative loss basis. Delegated risk defaults below specify action rules; per-idea/aggregate stress calibration and tax basis remain unverified. |

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
