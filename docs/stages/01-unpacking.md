# Stage 1 — Unpacking / Reframing

Date: 2026-10-07. Evidence: four user attachments plus answers recorded in `../decisions.md`. This is framing, not deep research, requirements approval or architecture. Current broker/data facts are not independently verified. All suggestions below remain proposals unless marked confirmed.

## Decisions and remaining decision-critical questions

### A-01 — Same account: confirmed, not ring-fenced
The test is on the same account as the core LQDT holding. Internal allocation does not establish segregation or protection from broker actions. Verify which balances, securities and linked accounts may support margin or be liquidated under the actual agreement. Until then, do not claim that exposure of the core portfolio is limited to the test allocation.

### A-02 — Plan versus authority: confirmed approval gate
RUB 30,000 includes margin, variation-margin liquidity and reserves. The user allows proposals above that allocation, but each increase requires approval. Distinguish planned allocation, approved available capital, absolute maximum exposure and broker-required funds. None is interchangeable with maximum loss. No emergency-funding automation is authorized.

### A-03 — Realistic virtual portfolio: confirmed
Attempt all eligible signals without discretionary skips, using comparable execution delays. Record every signal and outcome, including expiry, market closure, capital rejection, unfilled orders and partial execution. All signals being accounted for does not mean every signal becomes a filled trade. No unlimited-capital parallel portfolio is requested.

### A-04 — Loss tolerance: confirmed range, incomplete control policy
The user accepts RUB 10,000–15,000 final loss across several months and can provide emergency funds. This is not a per-position allowance or a peak-drawdown threshold. Do not pick a precise stop from the range. Gap moves, liquidity and broker actions mean an internal limit cannot guarantee an exact realized maximum.

### A-05 — Define what “loss” and “several months” mean — open
Is A-04 an absolute cash loss, underperformance versus LQDT, or both? What is the evaluation period? Also agree maximum interim drawdown, per-idea loss, aggregate stress loss, funding amount/delay and a hard escalation/stop rule. Proposal: always report both absolute and LQDT-relative outcomes. The choice of risk limits is not inferred from the word “medium.”

### A-06 — Define a human “trade” — open
Does one-to-several trades per day mean ideas, completed round trips, individual leg orders or all interventions including exits/adjustments? A spread requires multiple actions unless an appropriate broker-supported combined mechanism is available. Proposal: measure entry ideas and total manual interventions separately; the entry quota must not suppress necessary risk exits. Ask about night/unavailable periods and practical ability to respond to margin emergencies.

## Reframing the investment problem

### A-07 — Statistical spread trading is not guaranteed arbitrage
Separate contractual convergence mechanisms from statistical relative-value hypotheses. A high correlation or deviation from an average does not establish convergence, profit or a bounded loss. Identify the economic relationship, basis risks and failure modes before optimizing parameters. Some pairs may never qualify; zero signals is valid.

### A-08 — Broad coverage is an inventory goal, not an all-pairs mandate
Start research from the full instrument universe, then assess economic compatibility, dates, contract multipliers, currencies, delivery rules and liquidity. Exhaustive pair/parameter search increases false discoveries and computation. Calendar, related-underlying and cross-asset relationships need separate justification. No pair family is selected yet.

### A-09 — RUB 30,000 is a feasibility experiment
Integer contracts, hedge ratios, two-leg costs, margin and reserve needs may make even a minimal hedged position unsuitable. Never reduce required reserves merely to produce an executable signal. A valid finding is that the test budget supports only data collection/virtual testing or a narrow subset of instruments.

### A-10 — Scaling is a separate hypothesis
Success with a tiny test does not establish capacity at RUB 10–20 million. Recheck depth, turnover, impact, concentration, margin stress and exit capacity at each scale. Avoid extrapolating small-sample return percentages to the full portfolio.

### A-11 — Define LQDT comparison by a counterfactual — proposal
Compare a strategy-enabled account with the same account without futures, retained in LQDT, under identical genuine external cash flows and a consistent valuation convention. Report the RUB wealth difference and reconcile strategy-attributable cash flows. Do not let unrelated account activity or the large core holding hide test losses. In isolated reporting, reproduce the same difference through an incremental strategy ledger rather than changing the economic benchmark.

Cash flow classification:

| Event | Comparison treatment |
|---|---|
| Internal LQDT sale to fund margin/negative VM | Not an external contribution or performance gain; subsequent forgone LQDT return and sale/re-entry frictions matter. |
| LQDT remains held as eligible collateral, **if verified** | Its continuing return is earned in the strategy account too. Do not subtract that return again merely because units are encumbered. Encumbrance/liquidation risk is separate. |
| Daily VM receipt/payment | Economic futures gain/loss reconciled once; do not count both VM and the same cumulative unrealized P&L. Include cash return/idle drag consistently. |
| Top-up from existing account assets | Internal reallocation, not fresh profit or a reset of the loss clock. Log approval and risk-envelope usage. |
| Genuine new money from outside the account | Match the same amount and timestamp in the benchmark; identify funding origin/cost and the increase in risk budget. |
| Fees, funding, taxes where applicable | Apply consistently to actual and counterfactual paths; distinguish incremental from already-incurred fixed costs. |

Do not subtract an additional margin-times-LQDT hurdle if the counterfactual wealth difference already captures that drag. Margin is collateral, not a purchase price; notional is exposure, not cash invested. Track capital use and risk alongside RUB excess profit; label any ratio's denominator. LQDT yield varies through time. Loss of the LQDT long-term holding deduction is excluded as explicitly instructed, but other tax assumptions remain open. Net fund-price performance already reflects embedded fund expenses; do not charge them twice.

### A-12 — Waiting is allowed; sunk losses do not justify waiting
Entry and continued holding are different decisions. Assess forward expected outcomes from current prices, remaining costs, liquidity, margin stress and competing opportunities. Duration alone is not a forced-exit reason, but expiry, delivery, financing changes and model failure can be. Historical mean reversion does not prove that holding indefinitely is rational.

### A-13 — Flexible sizing and scale-ins are competing hypotheses
Compare fixed/minimal size, bounded adaptive size and predeclared scale-in rules under identical out-of-sample conditions. Never increase size merely to recover a loss. Incorporate correlated positions, model uncertainty and reserve depletion. Recommendation above the planned cap must show changed worst-case funding exposure and require A-02 approval; estimated high win probability alone is insufficient.

### A-14 — “Signal strength” is not a win probability
Separate effect size, evidence quality, calibrated outcome probability, expected net RUB excess, tail loss and holding-duration uncertainty. Specify the outcome and horizon behind any probability. Sparse medium-term trades may not support credible probability calibration despite a large number of one-minute bars. If unvalidated, report uncertainty rather than an invented confidence percentage.

## Execution and realistic evaluation

### A-15 — Delay model is not determined by three summary numbers
Known reaction range: 3–90 minutes; typical median approximately 5–15 minutes. Distribution shape and whether stress/illiquidity causes longer delays are unknown. Proposal: test multiple plausible delay scenarios before fitting actual timestamped behavior. Use order/fill timestamps in later shadow operation; distinguish reading, decision, placement and fill delays. Do not turn “median 5–15” into a uniform distribution by default.

### A-16 — Validity must be state-dependent
An age limit alone is insufficient. Recheck executable leg prices, costs, available capital, margin, trading session, model status and current expected excess after the delay. A signal should explain its invalidation conditions and require fresh information before a late entry. Stale or missing safety-critical information must not silently pass the check.

### A-17 — Two-leg execution can break the hedge
Independent leg fills, partials, cancellations, price moves and order rejection create temporary outright risk. Assess execution sequence and recovery policies as part of feasibility, not just a fee adjustment. Do not assume simultaneous fills at candle close or at a historical midpoint. Combined/spread orders and associated offsets require broker-specific evidence.

### A-18 — Monitoring 24/7 is not trading 24/7
User availability on weekends does not prove an instrument is tradable then. Verify dated calendars, clearing pauses, holidays, special sessions and broker access separately. Keep market timestamps and operational timestamps unambiguous. A notification outside a valid session must not claim an executable market opportunity.

### A-19 — Virtual accounting must reproduce constraints
Respect finite approved capital, concurrent positions, cash timing, margin changes, integer contracts, competing signals and close-out costs. Record rejected and stale ideas as outcomes rather than removing them from statistics. Deduplicate repeated notifications of the same idea; otherwise one persistent spread deviation can create fictional repeated entries. Portfolio results are distinct from trade-level averages.

### A-20 — Manual and automated execution are different experiments
A model viable with future BCS automation may not survive manual VTB delays, and vice versa. Keep execution assumptions and tariff applicability explicit. Research API access, tokens, order life cycle, reconnect/reconciliation, rate limits, paper-testing capability and permissions later. Do not assume free market data access or unattended trading rights from API availability alone.

## Evidence and validation risks

### A-21 — Finest free bars are a question, not an answer
“Free” does not imply real-time, deep history, complete coverage, usable licensing or no quota. Minimum candle interval is not proof of bid/ask availability. A single-sample live API response is not a history audit. Ask research to establish field availability, timestamps, delays, coverage, corrections, costs and storage/processing size before selecting a default interval.

### A-22 — Execution evidence sets the strength of conclusions
OHLCV cannot generally reconstruct spread crossing, intrabar order sequence, historical depth or realistic partial fills. Where quote history is missing, use explicit conservative scenarios and limit the claim to exploratory evidence. Do not disguise a proxy as a verified executable backtest. Additional fields should earn inclusion by incremental out-of-sample value.

### A-23 — Point-in-time instruments and events matter
Use real contract histories and specifications; account for delisting, expiry, rolls, delivery/cash settlement, trading limits, multiplier changes and historical margin where obtainable. Continuous adjusted series are not directly tradable contracts. Economic links may depend on dividends, funding and currency conversion. Event publication/availability time differs from ex-date or later corrected values; no future information may leak into a historical decision.

### A-24 — Broad optimization invites overfitting
Track all tried pairs, families and parameter sets. Use chronological out-of-sample evaluation, nested selection where needed, adequate separation for overlapping holding periods and a final untouched evaluation period. Compare simple baselines before complexity. Few independent episodes and wide uncertainty are a legitimate rejection reason. Do not optimize on the final test then continue describing it as out-of-sample.

### A-25 — Regimes, news and neural models stay optional
Regime conditioning, text features and neural policies require incremental evidence versus simple deterministic approaches. Regime labels must be available at decision time, not assigned from future data. Text requires publication and ingestion times, revisions and provenance. Free API/inference limits, including laptop resources, do not justify premature dependencies. No regime engine or LLM is an architectural requirement yet.

### A-26 — Validate operational survival, not just mean return
Evaluate loss paths, funding needs, margin shocks, gaps, spread breaks, correlated drawdowns, broker restrictions, stale data, outages and delayed human reaction. Distinguish a profitable terminal outcome from a path that would have triggered forced liquidation first. Record whether the model survives without unapproved rescue capital. No backtest can establish an absolute guaranteed loss ceiling.

## Broker, cost and resource constraints

### A-27 — The attached tariff is evidence with limits
The VTB PDF page 5 states zero base derivatives broker commission through 2026-12-31; exchange charges remain instrument-specific. Pages 2–4 describe separately charged advisory services. Clarify the user's active services and applicable agreement before choosing a cost model. Confirm promotion end/replacement conditions and historical tariffs. See `../inputs/README.md` for exact evidence locations; these are not independently verified current charges.

### A-28 — Full costs need a cash-flow boundary
Include opening and closing both legs, rebalancing/rolls, broker and exchange/clearing charges without duplicating bundled components, spreads, slippage/impact, delivery or exercise-related charges where relevant, actual borrowing, settlement/custody charges where incremental, LQDT liquidation/re-entry and tax timing if in scope. Avoid automatically applying securities overnight borrowing rates to futures notional. Proposal: show base and stressed costs with every performance claim.

### A-29 — KPUR and qualified-investor status are distinct evidence questions
Verify each broker's recognition of status, accessible contracts, initial/maintenance margin, risk rates, limits, collateral, portfolio offsets, forced close-out and notification/funding rules against applicable dated broker/exchange/regulatory documents. Do not assume KPUR grants lower margin, discounts or particular tools; exchange-level offsets need not be passed through to a client. Ask the user for redacted account-specific confirmations later.

### A-30 — Free infrastructure is constrained, not empty
The supplied VPS specification reports 1 vCPU, 1 GB RAM, 2 GB swap and 20 GB disk, with live VPN and existing digest workloads. Protect those services and their ports/settings. A stated 250–350 MB background-service ceiling does not imply another independent 350 MB is free: stage 3 must measure concurrent headroom and obtain a total budget. No deployment or remote probing in this stage. Heavy optimization and full-universe fine-grained history may not fit locally or remotely without batching/retention decisions.

### A-31 — Lightweight runtime and heavy research have different needs
Separate continuous monitoring needs from occasional optimization as a conceptual boundary only. Benchmark actual time/memory/disk costs before degrading time resolution. Minimize unnecessary services; modularity should support named extensions, not a speculative multi-service platform. The stack, database, UI framework and deployment mechanism remain unselected. Dark-only UI and Telegram delivery are confirmed brief requirements.

### A-32 — Security and reliability are part of the investment risk
A missed margin alert or duplicated signal can matter more than UI polish. Plan stale-data behavior, notification deduplication/retry, recovery and reconciliation later. Keep Telegram token/chat destinations and brokerage secrets out of source control. The attached GitHub credential was redacted in the repository; recommend rotation because it was shared in a document. Use minimal permissions for future unattended services and separate read-only monitoring from trade authority.

## Questions that can remain open until their stage

### A-33 — Clarify manual workflow and economics
Still needed: A-05 loss definition; A-06 workload; tax residency/account treatment; active advisory subscriptions; actual margin/collateral arrangement; source, ceiling and arrival time of rescue funds; overnight unattended exposure; maximum useful holding horizon; acceptable measured risk/return trade-off versus LQDT. None of these is answered by assumed residence or the uploaded tariff alone. Account identifiers and unredacted statements are not needed now.

### A-34 — Previous projects are a deliberate revision gate
The user will supply earlier MOEX projects in stage 7. Extract especially dividend/event sources, timestamp semantics, research conclusions, failed approaches, reusable data/code and licensing constraints. Explicitly revisit requirements, architecture and source selections then. Do not freeze them irreversibly beforehand.

### A-35 — Success must permit “do not trade”
Proposal: success first means a reproducible, honest decision process capable of rejecting every candidate after costs, delay and risk. A 30k live test is more useful for execution/operational validation than for proving long-run profitability. Advancement to real signals or automation should require evidence and separate authorization, not completion of a UI or a positive in-sample chart.

### A-36 — Stage exit and next turn
This stage delivers the reframing artifact, confirmed answers and grouped blockers. It approves neither a strategy nor safe real trading. The next stage is a list of investigations and well-formed prompts for research performed by the user, including any justified book/channel requests. Unresolved questions may be answered in parallel or carried explicitly; their answers must update the decision register rather than silently becoming defaults.
