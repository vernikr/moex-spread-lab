# Provisional pilot risk policy

Version: 2026-10-26, stage-5 architecture clarification. **Assistant-selected operational defaults under the user's delegation; not statistically validated limits or trading authorization.** Research R01/R05/R07/R08 may tighten or replace them with an explicit revision. This document defines research scenarios now, not running software or broker controls.

## Authority and numbers

| Control | Default | Meaning |
|---|---:|---|
| Planned test allocation | RUB 30,000 | All test resources: margin, VM liquidity and other reserves, not margin alone. |
| Interim drawdown warning | RUB 5,000 | Warning on the benchmark-relative curve; reassess funding, data quality and remaining risk. |
| Maximum interim drawdown trigger | **RUB 10,000** | From the highest observed benchmark-relative result, including the initial zero. Pause entries/additions; reduce risk in an orderly way and review. |
| Cumulative underperformance action trigger | **RUB 10,000 below LQDT** | Stop opening/adding; initiate an orderly exit of the pilot's risk, not a wait-until-recovery plan. |
| Outer cumulative underperformance boundary | **RUB 15,000 below LQDT** | Upper edge of the user's acceptable final shortfall. Exceeding it is a risk incident, not a permitted overshoot. It is not permission to trade until it is reached or a guaranteed maximum. |
| Maximum emergency additional funding | **RUB 15,000 cumulative gross** | Total positive additional allocations across the entire pilot, not per position, day or margin event. Requires explicit approval for each use. |
| Gross contributed test-funding envelope | **RUB 45,000** | Initial 30,000 plus at most 15,000 additional funding. Not a notional, current equity, maximum collateral exposure or maximum-loss cap. |
| Formal review | 90 calendar days from pilot activation | Planning default to make “several months” measurable. Review, not compulsory expiry of every position; no reset of losses, peaks, funding usage or approvals. |

The 10,000 trigger uses the lower edge of the user's tolerance to leave a 5,000 action buffer before the 15,000 boundary. This is judgment, not evidence that a 5,000 buffer covers gaps or a 90-minute response. The additional funding cap is half the initial allocation: deliberately finite liquidity support, not a doubling/recovery scheme. If the smallest feasible spread cannot satisfy a validated risk/funding test inside these envelopes, choose **no trade**, not larger defaults without revision.

## Metrics and cash-flow boundary

Use the whole-account comparison from A-11 or an exactly reconciled strategy-attribution ledger; do not subtract the entire core LQDT portfolio from a small sleeve.

- `V(t)`: actual strategy-enabled wealth, including futures economic P&L, cash and holdings, all realized costs and an estimate of immediate liquidation costs.
- `B(t)`: value of the matched no-pilot counterfactual, on the same valuation boundary, with identical genuine external cash flows and identical exogenous core-account actions. Assets diverted solely for the pilot remain in LQDT in this path. Do not mirror pilot-induced core sales or forced liquidations into the benchmark: their consequences belong to the pilot. If unrelated core actions or shared-margin interactions cannot be reconciled, flag attribution as unresolved rather than publish a clean excess-return claim.
- `X(t) = V(t) - B(t)`, with `X(0) = 0` before incremental test costs.
- `H(t) = max(0, max X(s) for s <= t)`; `D(t) = H(t) - X(t)`.

Use synchronized, validated, reasonably executable marks including exit costs. Monitor intraday whenever reliable data permit; do not rely solely on end-of-day figures. Stale/misaligned prices are not a reason to declare a limit safe; suspend new risk and apply the data-incident process. A bad tick must be investigated, not turned mechanically into a permanent peak. Preserve the original observations and any correction audit trail. Exact mark freshness rules await data/execution research.

For the provisional numeric policy, report `X` **before investor-specific income taxes**, after trading/funding costs. Show a separate after-tax scenario once the actual tax position is known; do not assume tax residence, relief, or tax-neutral liquidation of LQDT. LQDT fund expenses already embedded in its price must not be subtracted again. Loss of the LQDT long-term holding deduction remains excluded per the brief.

Funding events must not improve `X` mechanically. An actual external contribution raises both paths equally. An internal reallocation from core LQDT is not an external whole-account cash flow; the counterfactual keeps those units and the subsequent return difference remains visible. In a reconciled sleeve ledger, record matched funding flows rather than treating new allocations as profit. Existing units kept as collateral, if eligible, continue earning their actual return: do not charge a second fictitious LQDT hurdle.

Examples (after costs, at synchronized marks):

| Prior peak X | Current X | D | Consequence |
|---:|---:|---:|---|
| 0 | -5,000 | 5,000 | Warning. |
| 0 | -10,000 | 10,000 | Both action triggers; orderly risk exit. |
| +8,000 | -2,000 | 10,000 | Peak-drawdown trigger even though cumulative shortfall is only 2,000. Pause/reduce/review. |
| +20,000 | +10,000 | 10,000 | Profit giveback triggers discipline although still ahead of LQDT; this is an assistant-selected conservative pilot rule. |
| 0 | -16,000 after a gap | 16,000 | Outer boundary breached; immediate risk incident handling. The policy never guaranteed a 15,000 realized cap. |

A matched top-up at X = -8,000 leaves X = -8,000 immediately before its own transaction costs. It neither erases losses nor restores risk capacity.

## Actions, funding and resumption

1. Earlier broker, margin, expiry/delivery, naked-leg or data-quality danger overrides these monetary triggers. Do not wait for a numeric threshold before addressing an unsafe position.
2. At `D >= 10,000`: no new entries or exposure-increasing adjustments. Reassess executable exits, reduce risk in an orderly way, and require explicit user review before resuming. If `X <= -10,000` too, the cumulative-loss rule applies.
3. At `X <= -10,000`: end new risk-taking for this pilot and initiate a controlled exit of its positions. Response must consider the danger of leaving one leg unhedged; no unconditional assumption of instantaneous fills. Holding merely in hope of recovering the shortfall is not an alternative to the stop policy.
4. A top-up can prevent forced/disorderly liquidation or complete orderly de-risking of **existing** positions. It cannot finance fresh entries, averaging down, a higher trade quota, or an extension of the loss limits. It must not delay required risk reduction. At `X <= -10,000`, any approved funding is solely to reach flat safely, not to wait for recovery. Every proposed funding event needs a fresh economic-risk and liquidity-path check; approval does not erase a failed risk test.
5. Count positive transfers into the strategy allocation toward the lifetime 15,000 cap, whether sourced from core LQDT, other cash or outside the account. Count market sales by the **cash earmarked**, not gross unrelated account turnover. Count deliberately newly encumbered core collateral by the earmarked amount as well; reject unclear allocation/valuation rather than exploit a cash-only loophole. Broker-initiated allocation, encumbrance or sale of core holdings caused by the pilot must also be recorded against this gross allowance, even without user approval; log it as an unauthorized funding incident, not an exemption. A market sale is not itself a loss equal to its proceeds: include its actual fees, economic loss and subsequent LQDT return difference in X under the matched accounting rules. Do not double-count funds as a cash transfer after already counting the corresponding collateral allocation. Broker recourse beyond the designation remains a separate unresolved risk.
6. Withdrawals, reimbursements, returned emergency funds and profits do not replenish the gross top-up allowance. Example: 10,000 used, returned, then 5,000 used exhausts the cap. Retained strategy earnings are P&L, not new funding, and do not automatically authorize increasing the entry capital envelope.
7. Entry and exposure-increasing sizing must pass with **zero emergency funding**, using only the original 30,000 envelope less actual losses, reserves and other commitments. A previous rescue allocation or retained profit does not expand that entry authority. Do not count a proposed top-up as available when sizing an entry. For virtual execution, require an explicit recorded funding authorization and model credit time; otherwise use no additional funding. If approval cannot arrive in time, the default is to reduce/exit risk, never inject unapproved funds. Nonavailability is a valid reason to reject or unwind. No automatic sweep of core LQDT is authorized.
8. An approval to inject money is not an approval to restart trading or increase size. Crossing 30,000 still requires the A-02 per-event approval. Selecting the numeric ceiling here does not replace that approval.
9. Review dates and restarts do not silently reset `H`, `X`, or funding consumed. Any changed limit/new pilot requires an explicit versioned decision and continuous lifetime reporting.

### Exposure ladders

- A **scale-in** is any action that increases the absolute exposure of an existing spread. It is permitted for research only as a predeclared ladder step with a fixed trigger, quantity, aggregate cap and invalidation rule. A worsening spread may trigger the step, but worsening alone is never sufficient: current executable data, costs, margin, VM liquidity, reserve, competing commitments and stressed exit path must pass again.
- A scale-in is exposure-increasing sizing and therefore uses only the remaining original pilot envelope. Emergency funding, proposed top-ups, retained profits and automatic core-LQDT transfers cannot finance it. The action is blocked at `D >= 10,000` or `X <= -10,000`, and earlier broker, margin, legging, expiry or data danger overrides these thresholds.
- A **staged exit** is a predeclared or risk-forced reduction in target exposure. Each reduction has its own two-leg execution lifecycle; partial reduction leaves the remaining position explicitly recorded. Risk-forced reduction may bypass the profit-taking ladder, but may not silently create a new scale-in.
- A strategy may be rejected if its minimum ladder step cannot satisfy the same finite-capital and stress constraints as a single-entry strategy. The ladder is a testable hypothesis, not a presumption that averaging into a widening spread is safe or profitable.

## Preconditions and still-unvalidated controls

These numbers do **not** establish safety. Before real trading:
- R01 must establish broker collateral/recourse and liquidation rules. A shared account may expose the core LQDT holding; 45,000 is an internal funding rule, not broker segregation.
- Real activation requires evidence that core recourse is bounded, or that the adverse shared-account recourse/funding path can itself be kept inside the stated envelope under a documented conservative stress test. Unknown or unenforceable recourse means no trade; a hypothetical future approval is insufficient. No statistical stress test guarantees protection from all broker/gap events.
- A pre-trade and ongoing funding check must exclude the core LQDT collateral value from pilot-available resources unless separately and explicitly approved within the envelope. If broker auto-collateralization makes that impossible to enforce, the shared-account arrangement must be revisited before live activation. A broker-initiated earmarking beyond the allowance is a breach, not a way to enlarge it. Research may recommend segregation, but the current user choice is not silently changed.
- R07 must establish per-idea and joint-portfolio stress limits, reserve policy, integer-size feasibility and capital headroom through the manual response/exit interval. Study a 5,000 stress-buffer hypothesis and a 50% initial-margin-use hypothesis, but do not pretend either is a proved bound or a confirmed requirement.
- Research must turn residual risk headroom into an enforceable sizing/continuation test at entry and every reliable risk review, not only after a trigger: `X - stressed incremental loss - error buffer >= -15,000`. Here, stressed incremental economic loss through reaction and exit, including remaining liquidation frictions without double-counting marks, plus a positive model-error buffer must fit inside the remaining LQDT-shortfall budget. Separately test the cash/margin funding path; entries use zero emergency funding. Only subsequent orderly de-risking may use separately approved, actually available emergency funds. At the -10,000 action point only 5,000 remains to the outer boundary; if that cannot be supported by evidence, reduce size, act earlier or reject the trade. These stress tests are not guaranteed tail bounds.
- Do not equate financial hedge efficacy with exchange/broker margin-offset eligibility; test economic stress and funding stress separately, including offsets being withdrawn.
- R05 must verify accounting examples and tax sensitivity; R08 must verify delayed/partial execution and reliable stop monitoring.
- Actual emergency funding origin, settlement delay and user unavailability remain unknown. A promise of emergency funds is not a substitute for prefunded survival liquidity.

Actual losses can exceed all internal numbers, including the amount allocated. If the required controls cannot be verified on the shared account, live feasibility remains blocked; research and a constrained virtual pilot can continue.

## Additional policy consistency cases

| Event | Required interpretation |
|---|---|
| X gaps from -6,000 to -14,000 during reaction delay | The cumulative action trigger has been crossed; no new risk, initiate orderly exit immediately when observable. Only 1,000 remains to the outer boundary; no assertion that the initial buffer was adequate. |
| Broker sells/earmarks 20,000 of core LQDT for pilot funding without approval | Record 20,000 additional funding usage (not automatically a 20,000 economic loss), exceeding the 15,000 cap. Funding/authority breach; notify and de-risk, reconcile X including actual induced costs/losses. |
| User is asked to add funds while X=-11,000 | Only propose the minimum needed for orderly flattening, within the remaining gross cap and with explicit approval. It cannot fund continued risk-taking. If approval is unavailable, do not inject money automatically. |

These are policy-definition checks using hypothetical values, not simulated market performance or executed broker actions.
