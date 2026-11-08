# Decision register

Updated 2026-11-08 after the user's screener-first product direction. User answers override proposed interpretations; project assumptions and delegated defaults remain distinct from verified external facts.

| ID | Confirmed user decision | Boundary / unresolved part |
|---|---|---|
| A-01 | Initial test operates on the same account as LQDT. | No legal/operational ring-fence inferred; collateral and broker recourse unverified. |
| A-02 | Exceeding the planned RUB 30,000 requires explicit user approval. | The delegated default permits proposals for at most 15,000 additional gross funding over the whole pilot, for existing-position liquidity/de-risking only. Each use still needs explicit approval. Emergency availability and delegated ceiling selection are not approval to transfer money. |
| A-03 | Use one realistic virtual portfolio. Attempt all eligible signals without discretionary omission; record constraints and reasons for nonexecution. | No fictional fills when price, capital, tradability or validity prevents execution. No second unconstrained portfolio requested. |
| A-04 | Acceptable final **underperformance versus LQDT** over several months: **RUB 10,000–15,000**. Emergency top-ups are possible. | The user resolved the numeric wording and then the benchmark-relative loss basis. Delegated risk defaults below specify action rules; per-idea/aggregate stress calibration and tax basis remain unverified. |
| A-05 | For this private, single-user pet project, treat freely accessible public market data as available for the project's local research, storage and calculations; do not block the research pipeline on unresolved ISS-use terms. If better historical contract evidence is unavailable, use the current contract parameters as an explicitly labelled fallback assumption. | This is a project operating assumption, not a legal opinion or proof of MOEX permission. It does not create redistribution, public-service, broker, live-trading or unattended-runtime authorization. Historical executable bid/ask is excluded under A-08. |
| A-06 | For medium-term analytical research, treat the close of an ISS candle (1m/10m/1h/1d) as the project's working price proxy for an available market price at that interval. Use the proxy with delay and cost sensitivity; do not present it as a reconstructed historical bid/ask or guaranteed fill. | This is a modelling assumption for exploratory analysis. It does not resolve intrabar order sequence, two-leg synchronization, queue position, partial fills or execution price uncertainty. |
| A-07 | Use the inspected reference methodology for a two-layer LQDT benchmark: observed `VTBM → LQDT` and an explicitly labelled extended proxy `CBR_RUONIA → FXMM → RUSFAR → VTBM → LQDT`, stitched by returns after VTBM split correction. | Proxy periods are not actual LQDT prices and cannot replace the observed benchmark for a clean account-level conclusion. Missing proxy source rows remain missing; no rate or price is invented. |
| A-08 | Historical order-book, bid/ask and actual-fill data will not become available in this project. Continue with candle closes as labelled analytical price proxies. For the user's medium-term manual reaction, the close-to-book difference is treated as secondary to reaction delay and covered by delay/cost sensitivity. | The project does not claim reconstructed fills, queue position, two-leg simultaneity or guaranteed prices. This is no longer an offline-research blocker; live execution evidence remains a separate gate. |
| A-09 | The user's current VTB tariff is `Мой Онлайн Прайм`, evidenced by the supplied PDF. VTB broker commission for derivatives is zero through 2026-12-31 inclusive; exchange fees remain instrument-dependent. | The PDF does not establish margin, collateral eligibility, KPUR treatment, liquidation rules, post-2026 pricing or whether optional Advisory services are connected. |
| A-10 | The user confirms that the VTB account uses EBS and that KPUR is assigned. Use these as account facts in the working risk profile. | The exact EBS perimeter, current LQDT treatment, individual coefficients and current client display remain unverified; the profile must use labelled approximations and must not treat core LQDT as pilot cash. |
| A-11 | Comparison rule (2026-11-08): the benchmark portfolio holds the whole allocation in LQDT. The studied portfolio may use the allocation freely: any futures size the user chooses and the broker allows, with all remaining cash held in LQDT. | Replaces the stand-alone 1-contract/idle-cash accounting for new runs; existing S1–S2 numbers use the old accounting until reimplemented. Margin cash is assumed not to earn LQDT unless collateral eligibility is evidenced; position size, margin limits and variation-margin funding from LQDT must be explicit parameters. |
| A-12 | Execution and cost rules (2026-11-08): the user's reaction time (3 min–1.5 h, brief) rules out day-long delays, so daily research fills at the next open (or uses a shorter timeframe). LQDT is not used as collateral because the charge consumes its yield. Costs are the published exchange tariff plus slippage as a parameter; the per-leg-day funding charge is removed. Volume is not a strategy input: a pair is either tradable on every day of its window or excluded. | The 100-contracts-per-leg-per-day threshold is an assistant-selected working value (`MIN_DAILY_VOLUME`). Tariff group assignment for metals, RGBI and BTC is an assumption. Open prices of thin legs may be noisy; fills remain proxies, not bid/ask evidence (A-08). |

## Product direction confirmed 2026-11-08

The user prioritizes a private research screener over a signal-first workflow. It must let the user select a futures pair, strategy and parameters, inspect the resulting strategy and matched LQDT wealth curves and return/risk measures, and automatically search parameters for a chosen pair or basket of pairs. The user wants a minimal visible screener soon. The saved `СКРИНЕР_epub.md` is a detailed product hypothesis, not evidence of an existing product or validated trading rules.

The user subsequently specified a table-first interface (2026-11-08): each row is a pair with source context, trade count, annualized performance versus LQDT and percentage risk, never absolute RUB UI metrics. One strategy and its parameters apply to all pairs; row checkboxes select pairs for automatic search of a stable parameter plateau. S2 implements this across independent pair studies. It does not override A-03's single shared-capital virtual portfolio for future basket performance.

This revises the order of Stage-9 delivery, not the research/live boundary: first a local dark-only analytical interface and transparent experiments, then guarded parameter search and portfolio-level basket evaluation; monitoring, Telegram, broker integration and trading remain separately gated. A good-looking backtest, including the reference SBRF calendar-spread result, is not approved performance. The original brief remains the source record; current priorities are in Stages 4–8.

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
