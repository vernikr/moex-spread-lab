# Stage 2 research pack

Prepared 2026-10-07. **Prompts only: no investigations run and no conclusions verified.** Each file in `prompts/` is a standalone copy/paste request including project context and evidence requirements. Reports should use `response-template.md`; a private repository link alone may not grant the research tool access.

## Start here

1. Send **R01, R02, R04, R05, R06 and R09** as separate research requests; they can run in parallel. For R02 attach `../inputs/prof_privilegiya.pdf`; it is evidence to verify, not proof of current account applicability. R01/R05 need only redacted account information if public evidence is insufficient.
2. Return available reports before attempting to make every finding agree. These six resolve collateral, costs, data, benchmark, candidate universe and the validation standard. A decisive feasibility failure should pause dependent work, not cause invented inputs.
3. Use their conclusions for **R07, R08, R10 and R11**. Run **R12** early if equity/index-linked pairs remain candidates. R11 is desk research/a proposed benchmark plan only; actual installation and VPS checks belong to stage 3.
4. **R03** may run early in parallel but is mandatory only before designing/enabling automated BCS execution. Do not choose architecture that assumes unverified API features.
5. **R13–R15** are optional extensions, deferred until simpler deterministic approaches have evidence. R12's event timing is not deferred with optional NLP.
6. Return reports as `Rxx-YYYY-MM-DD.md`, with evidence and tables. Bring partial/blocked reports too. A book chapter or channel export is supplementary evidence, not a substitute for current official broker rules.

## Inventory and dependencies

| ID | Investigation / prompt | Priority / dependencies | Decision enabled |
|---|---|---|---|
| R01 | [Broker rules, KPUR, collateral and liquidation](prompts/R01-broker-risk.md) | Wave 1; none | Can shared-account real testing be safe and within the funding authority? |
| R02 | [VTB/BCS full cost comparison](prompts/R02-costs.md) | Wave 1; none, reconcile R01/R06 later | Costs to use by date, contract and scale; tariff switches |
| R03 | [BCS Trade API and automation constraints](prompts/R03-bcs-api.md) | Parallel or later; before automation | Is a reliable authorized execution adapter feasible? |
| R04 | [Free market data and point-in-time coverage](prompts/R04-data.md) | Wave 1; none | Finest defensible interval, fields and limits |
| R05 | [LQDT accounting, funding and taxes](prompts/R05-benchmark.md) | Wave 1; none, reconcile R01/R02 | Correct net comparison and risk-curve accounting |
| R06 | [Futures universe and economic spread families](prompts/R06-universe.md) | Wave 1; none, annotate missing R01/R04 | Eligible candidate families rather than blind all-pairs search |
| R07 | [Pilot risk, margin liquidity and position sizing](prompts/R07-risk.md) | Wave 2; R01/R02/R04/R05/R06/R09 | Validate/revise provisional limits, smallest feasible position |
| R08 | [Manual fills, validity and virtual portfolio](prompts/R08-execution.md) | Wave 2; R01/R02/R04/R06/R09; exchange constraints with R07 | Credible delayed execution and nonexecution accounting |
| R09 | [Selection bias and validation protocol](prompts/R09-validation.md) | Wave 1; none | Predeclared standard of evidence before strategy selection |
| R10 | [Deterministic strategies and holding/allocation decisions](prompts/R10-strategies.md) | Wave 2; R04/R05/R06/R09; constrained by R07/R08 | Testable baseline hypotheses, not a profitable model claim |
| R11 | [Free-resource feasibility and resilient operations](prompts/R11-operations.md) | Wave 2; R04; add R03 for automation | Stage-3 measurement plan and input to stack decisions |
| R12 | [Dividends and other point-in-time events](prompts/R12-events.md) | Conditional core; R06 scopes it | Valid equity/index economics and later reuse requirements |
| R13 | [Market regimes](prompts/R13-regimes.md) | Optional; after R09/R10 baselines | Whether state conditioning earns its complexity |
| R14 | [News/text and channel evidence](prompts/R14-text.md) | Optional; R04/R09/R10/R12 | Timestamp-valid incremental text evidence |
| R15 | [Neural models under local compute limits](prompts/R15-neural.md) | Optional; R04/R09/R10/R11 | Whether neural approaches beat simple alternatives net of burden |

Dependencies are evidence inputs, not an instruction to fabricate missing reports. Independent parts may be researched early; dependent conclusions remain conditional. R07/R08 exchange stress and execution assumptions and need a consistency review, not a circular request to “finish the other first.”

## Shared evidence standard

- Current facts: use research execution date; do not reuse the pack date as today's date if run later. Historical claims need valid-from/valid-to and availability times.
- Primary issuer/regulator/exchange/broker/API documents first. Marketing summaries, forums, books and videos cannot establish account-specific permissions or charges.
- Each material claim: exact source, relevant page/section, date/version, scope, short supporting excerpt, and remaining uncertainty. No invented URLs, confirmations, measurements, access or citations.
- Separate verified current rules, historical evidence, hypotheses, recommended tests and unresolved account questions. Missing values are unknown, not zero/free/unlimited.
- No trades, credentials, paid services, production probes, live account operations or large data downloads requested. Small public read-only checks are optional if permitted; disclose whether actually run. Otherwise propose reproducible checks.
- Give decision tables, calculations and falsification criteria rather than a generic topic survey. Explicitly allow “not feasible” and “insufficient data.”
- Recommend architecture/stack only as evidence-backed alternatives for stages 5–6; do not start implementing them.

## Literature and channels: selective, not a mass-ingestion task

Specific reading leads, **not sources claimed to have been consulted**:

| Candidate work | Useful question | Prompt / priority |
|---|---|---|
| Ganapathy Vidyamurthy, *Pairs Trading: Quantitative Methods and Analysis* | Economic/statistical relationships, hedge estimation and failure modes; assess transfer to dated futures contracts | R06/R10; useful if present in the user's library |
| Larry Harris, *Trading and Exchanges: Market Microstructure for Practitioners* | Spread crossing, adverse selection, order types, liquidity and two-leg execution | R08; selected relevant chapters |
| Robert Carver, *Systematic Trading* | Position sizing, diversification, costs and risk discipline | R07/R10; compare with discrete tiny positions |
| David H. Bailey and Marcos López de Prado, *The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting, and Non-Normality* | What corrections do and do not solve under sparse dependent trades | R09; paper, obtain legitimate copy |
| Marcos López de Prado, *Advances in Financial Machine Learning* | Leakage-aware validation, overlapping labels and multiple testing; assess applicability rather than mandate the book's full machinery | R09/R15; targeted chapters only |
| Ernest P. Chan, *Algorithmic Trading: Winning Strategies and Their Rationale* | Simple deterministic hypotheses, execution/cost limitations, falsification | R10; targeted baseline extraction |

Ask for a contents/index search first, then only relevant excerpts. Identify actual edition and pages after access; do not invent page numbers. Extract hypotheses, assumptions, counterexamples and reproducibility needs, not generic summaries or promises of profitable implementation. Copyrighted books need not be copied into the repository.

For YouTube/Telegram, first identify **official MOEX, NCC, VTB and BCS** channels through their organizations' own websites. Exact handles are intentionally not asserted without verification. Prioritize dated technical/API/risk/tariff material over promotion. Ask for bounded exports around relevant episodes or selected technical series; a whole-channel export is justified only by a stated evidence gap and manageable cost. Preserve original post/message IDs, links, publication and edit/availability timestamps; transcript timecodes alone are not calendar dates. Anonymous signal channels cannot prove trade performance or historical availability. R03/R12/R14 specify when deeper channel work would help.

## Completion and acceptance

Stage-2 artifact delivery is complete when this inventory and usable prompts exist. Research is **pending**, not complete. Returned reports will be checked for traceability, chronology, missing account scope, contradiction, cost double-counting and data limitations before being accepted. Their findings can revise stage 1 and the provisional risk policy; later stage artifacts must reference the accepted evidence. Do not label unreceived reports as evidence or choose real-trading parameters from unsupported precision.
