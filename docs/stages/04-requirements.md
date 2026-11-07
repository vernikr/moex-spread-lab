# Stage 4 — Functional and business requirements

Date: 2026-10-26

## Status and scope

Stage-4 requirements are delivered and revised 2026-11-08 for the user-confirmed screener-first direction. The first visible product is a private research workbench, not a signal service. This document does not authorize trading or claim that a profitable strategy exists.

The first product must answer one question honestly:

> Given the data, costs, available capital and actual delay, is there a tradeable MOEX futures-spread opportunity that improves the user's wealth path versus keeping the relevant capital in LQDT?

The product must be allowed to answer **no opportunity**, **inconclusive** or **insufficient data**.

## Product boundary

### Screener MVP includes

1. A local, private, dark-only interface: select an admitted futures pair, compatible strategy, editable parameters, capital and labelled cost/delay scenario; run and compare saved configurations.
2. A chart of the strategy-enabled wealth path and the matched no-trade LQDT path, plus their RUB difference over time; show return, benchmark excess, drawdown, risk/capital needs, trade counts and open-position status on the same valuation boundary.
3. A small, explicit pair/strategy registry, beginning with the existing research candidates and a separately audited SBRF near/far calendar candidate from `calendar-spread`; no reference result is imported as accepted performance.
4. A bounded automatic parameter search for a selected pair and, after shared-portfolio composition is working, for a selected basket. Show the search space, all trials, train/validation/sealed-confirmation periods, nearby-parameter stability and the reason for any inconclusive result; never label the best in-sample point a validated winner.
5. Versioned market, contract, event and cost inputs; no-trade and deterministic baselines; one finite-capital virtual portfolio with delayed candle-price proxies, rejected steps and staged exposure where a strategy supports it.
6. Reproducible exports and experiment history, including assumptions, data vintages, missing evidence and source status. Monitoring and notifications are later extensions.

### MVP excludes

- real orders or broker API write access;
- automatic funding, liquidation or rescue actions;
- BCS execution;
- a public or VPS-hosted dashboard;
- unrestricted all-pairs discovery, an unlimited search or a purported universal optimal setting;
- text, regime and neural models as required components;
- a claim of profitability, safety or capacity at RUB 10–20 million.

## Business requirements

| ID | Requirement | Acceptance criterion |
|---|---|---|
| BR-01 | Compare every strategy against a flow-matched LQDT counterfactual | Reports show `V`, `B`, `X = V - B`, peak drawdown `D`, shortfall and costs on a common valuation boundary |
| BR-02 | Prefer rare, high-quality opportunities over forced activity | Zero-trade days are valid; no component is rewarded for increasing trade count |
| BR-03 | Respect the RUB 30,000 initial test allocation | Entry feasibility excludes emergency funding and respects integer contracts, margin, VM reserve, costs and concurrent commitments |
| BR-04 | Keep the core LQDT holding outside assumed pilot liquidity | The system never treats core collateral as available cash without an explicit scenario and evidence flag |
| BR-05 | Account for manual response from 3 to 90 minutes | Every signal has a decision-time revalidation and a recorded delay scenario |
| BR-06 | Preserve the user's one realistic virtual portfolio | All eligible signals are attempted according to a frozen priority rule; omissions, rejections and partial fills remain outcomes |
| BR-07 | Make a signal explainable | A report identifies the relationship, data vintage, costs, expected holding period, size basis, invalidation conditions and uncertainty |
| BR-08 | Permit a safe conclusion when evidence is weak | Missing data, costs, margin or fills produce an explicit block/inconclusive result rather than a zero assumption; the user's A-05 private-research assumption is recorded rather than treated as an unresolved local-pipeline blocker |
| BR-09 | Put pair/strategy/parameter exploration before signal delivery | A user can run and compare configurations locally, see the full LQDT-relative wealth/risk path and inspect why a configuration is blocked or exploratory |
| BR-10 | Search parameters without selecting on the answer sheet | For a selected pair or basket, record every trial and a declared search budget; tune only on earlier data, rank on later validation, and display untouched confirmation separately |
| BR-11 | Evaluate a basket as one portfolio | Joint capital, simultaneous exposure, priority, correlation/concentration and LQDT flows determine the basket curve; independent pair profits must never be simply added |

## Functional requirements

### Data and provenance

- **FR-01 — Source adapters.** The system must accept data through replaceable providers; no strategy may depend directly on one broker or ISS response shape.
- **FR-02 — Raw preservation.** Store the original response/file reference and a content hash before normalization where licensing permits local retention.
- **FR-03 — Three clocks.** Keep exchange event time, source publication/update time and local receipt time separately. Unknown times stay unknown.
- **FR-04 — Versioned specifications.** Version contract terms, lot size, tick value, settlement, expiry, trading calendar, risk parameters and fees with effective, publication and receipt dates.
- **FR-04a — Corporate-action provenance.** Store corporate actions as dated observations with action type, announcement/publication/availability time, record/ex/payment dates, amount, currency, source role, locator, content hash and conflict/supersession status. Unknown dates remain unknown.
- **FR-05 — Missingness.** Distinguish no observation, unavailable source, stale observation, rejected quality check and genuine no-trade state.
- **FR-06 — Point-in-time rule.** A decision may use only fields available at its decision timestamp. Later corrections are new vintages, not silent edits.
- **FR-06a — Price roles.** Keep settlement, analytical close, last-trade close, bid, ask and live mark as distinct observations. Each strategy/accounting operation declares the role it consumes; an absent role cannot be silently substituted.
- **FR-06b — Adjustment lineage.** Split/dividend-adjusted series are explicit derived views with source events, adjustment mode and vintage. A price-gap check may validate or challenge an event but cannot overwrite its primary date or amount.

### Universe and strategies

- **FR-07 — Explicit universe.** Candidate instruments and relationship families are configured and logged before evaluation.
- **FR-08 — Economic relationship.** Every pair has a declared relationship type: identity, calendar/carry, exchange-recognized spread, or a separately justified hypothesis.
- **FR-09 — Eligibility filters.** Apply contract status, expiry, trading session, liquidity, lot feasibility and data completeness before signal generation.
- **FR-10 — Baselines first.** Run a no-trade LQDT control and a small deterministic baseline before any adaptive, regime, text or neural component.
- **FR-11 — Trial registry.** Record every tested pair, family, parameter set, cost scenario and decision rule, including discarded trials.
- **FR-12 — Frozen evaluation.** Use chronological splits, overlap-aware purge/embargo, a sealed confirmation period and no post-result tuning of that period.
- **FR-12a — Exposure ladder specification.** A strategy that permits staged exposure must declare, before evaluation, its entry/add/reduce/exit thresholds, quantity at each level, maximum aggregate exposure, cooldowns, invalidation rules and risk overrides. The same specification must drive research, signals and virtual execution.
- **FR-12b — Search space and selection.** A strategy publishes parameter names, types, units, valid ranges/dependencies and a versioned default. A search declares pair(s), candidate grid/budget, objective and risk constraints before running. Every attempted, rejected and failed configuration is logged; a manual edit after viewing confirmation consumes a new experiment and cannot reuse that confirmation as untouched evidence.
- **FR-12c — Basket selection.** A basket declares constituent pairs, per-pair strategy and parameters, joint constraints, selection rule and priority before evaluation. Any pair or basket selection using historical performance is part of the search and must be validated out of sample; overlapping contracts and correlated risk are explicit.

### Signals and delayed execution

- **FR-13 — Signal identity.** Each signal has a stable ID, version, pair, legs, quantities, model version, data vintage and creation timestamp.
- **FR-14 — Executable revalidation.** At the simulated human decision time, recheck both legs, session, quote age, costs, margin, cash, validity and expected net advantage.
- **FR-15 — Explicit invalidation.** Signals expire on stale data, price movement, cost erosion, session closure, margin failure, missing leg or model invalidation.
- **FR-16 — Two-leg lifecycle.** Model submission, acknowledgement, fill, partial fill, cancellation, unknown outcome and hedge completion separately for each leg.
- **FR-16a — Step lifecycle.** Every exposure increase or reduction is a separate decision and two-leg lifecycle. A later step is never an implicit continuation of the original fill; it requires fresh point-in-time data, revalidation and a new risk check.
- **FR-17 — Conservative fills.** Do not infer a fill from a candle midpoint or a touch without a declared execution scenario. Unknown execution evidence remains unknown.
- **FR-18 — Manual workflow.** Measure notification, reading, decision, order and fill timestamps separately when shadow operation begins.

### Virtual portfolio and accounting

- **FR-19 — Single portfolio.** Maintain one event-sourced virtual portfolio with cash, positions, reservations, orders, fills, funding events and reconciliation corrections.
- **FR-20 — Integer and finite resources.** Enforce whole contracts, available RUB, margin, VM liquidity, reserve, simultaneous positions and competing signals.
- **FR-21 — LQDT ledger.** Reconstruct the no-pilot path with identical genuine external flows. Internal transfers and pilot-induced LQDT sales do not reset performance.
- **FR-22 — No double counting.** Reconcile variation margin, futures P&L, retained LQDT return, financing, fees and liquidation marks exactly once.
- **FR-23 — Risk path.** Report `X`, high-water mark, `D`, cumulative shortfall, absolute P&L, margin peaks, VM cash needs, legging exposure and emergency-funding usage separately.
- **FR-24 — Funding gate.** Entry uses zero emergency funding. A simulated top-up requires a recorded approval and credit timestamp and may only support orderly de-risking of an existing position.
- **FR-25 — Failure states.** The portfolio fails closed on stale safety data, unknown broker state, missing hedge, unavailable capital or unresolved reconciliation.

### Reporting and operations

- **FR-26 — Research report.** Every run produces inputs, versions, assumptions, trial count, rejected opportunities, fills, costs, benchmark path, sensitivity cases and limitations.
- **FR-27 — Signal explanation.** The user sees what changed, why the opportunity may exist, what must be true to enter and when it no longer makes sense.
- **FR-28 — Notification boundary.** Notifications are idempotent, retryable and deduplicated. Loss of Telegram delivery never authorizes a trade.
- **FR-29 — Operational outbox.** Future alerts are written durably before delivery and can be replayed without duplicate signals.
- **FR-30 — Recovery.** After a restart or outage, reconcile stored events and current source state before generating new entries.
- **FR-31 — Screener workspace.** A local dark-only view offers an admitted pair selector, compatible strategy selector, validated parameter controls, run/compare action and a visible result status. Unsupported pairs or missing inputs are disabled with a reason; a chart is not fabricated.
- **FR-32 — Comparable performance view.** Plot strategy wealth `V`, matched LQDT wealth `B`, excess `X = V - B` and drawdown on dated axes. Show initial capital, external flows, open positions, observed/proxy price roles, costs and whether the curve is exploratory, held-out or account-reconciled. Report return, excess return in RUB and %, peak-to-trough drawdown, worst shortfall, trade frequency and capital/margin stress; undefined metrics remain undefined.
- **FR-33 — Automatic pair/basket optimization.** Execute a bounded, reproducible search with shared strategy/risk/portfolio rules. Rank only eligible configurations by a declared LQDT-relative objective and risk constraints; show no-trade control, trial counts, validation versus untouched confirmation, parameter-neighborhood stability and stress sensitivity. A basket uses one chronological shared-capital ledger, not a sum of pair backtests.
- **FR-34 — Experiment comparison.** Save the exact pair/basket, strategy, parameters, dates, data/artifact versions, cost/delay assumptions, eligibility/rejection reasons and output hash for every run. Comparisons align capital, time window and valuation basis or flag them as non-comparable.

## Non-functional requirements

- **NFR-01 — Reproducibility.** A run is reproducible from versioned code, configuration, source vintages and trial registry.
- **NFR-02 — Fail closed.** Unknown safety-critical values block new exposure; they are never coerced to zero, free or unlimited.
- **NFR-03 — Auditability.** Corrections append new records and retain the prior observation; reports can trace a result to source and timestamp.
- **NFR-04 — Security.** Tokens, keys, account identifiers, client profiles and personal statements stay outside Git and logs.
- **NFR-05 — Resource isolation.** Heavy research runs on the laptop. Any VPS process is bounded, has an explicit memory cap and does not bind a new public port.
- **NFR-06 — Availability behavior.** Outage, stale-data and broker-unreachable states are visible and safe; the system does not pretend that monitoring equals execution.
- **NFR-07 — Modular extension.** Data providers, tariff/cost profiles, strategy families, execution modes, brokers, reports and notification channels are replaceable boundaries, not duplicated business logic.
- **NFR-08 — Dark-only interface.** The first screener view is dark-only and local/private; no public port or VPS deployment is required. It consumes the same run results as the CLI rather than implementing a second accounting engine.
- **NFR-09 — Contract-first modules.** A replaceable module declares a stable contract version, capabilities, inputs, outputs, dependencies, resource needs, data rights and owner. Modules communicate through domain contracts and events, not private tables or undocumented imports.
- **NFR-10 — Independent lifecycle.** A module can be enabled, disabled, upgraded or rolled back through a versioned run configuration. Historical runs retain the exact module and artifact versions used.
- **NFR-11 — Team isolation.** An independent team can develop and test a module using public schemas, fixtures and contract tests without access to credentials, unrelated private data or another team's implementation.
- **NFR-12 — Safe unknowns.** An absent, incompatible or revoked module capability blocks only the dependent operation and produces an explicit reason; it never silently falls back to zero cost, zero risk or a different artifact.

## Acceptance gates for implementation slices

The existing offline mechanics and the new screener/search slices must demonstrate the applicable checks on fixtures and real-data proxy runs before a stronger conclusion:

1. Rebuild the same LQDT counterfactual and `X` curve twice with identical output.
2. Reject an entry when one leg is stale, unavailable, unaffordable or outside its session.
3. Record a signal that becomes stale during the simulated 3–90 minute delay as a non-entry.
4. Record partial legging and require an explicit hedge/flatten decision; never create an invisible balanced fill.
5. Show that a genuine external top-up moves both paths symmetrically and does not erase the loss history.
6. Show that an internal LQDT transfer is not counted as profit and does not replenish the emergency allowance.
7. Produce identical results after restart and event replay without duplicate signals.
8. Generate a report containing no secrets and a clear list of unknown or unsupported inputs.
9. Apply a predeclared scale-in ladder only when its threshold, quantity, aggregate cap, capital, margin, reserve and stress checks pass; otherwise record a rejection.
10. Prove that an adverse move alone cannot create an unbounded scale-in or consume emergency funding.
11. Represent each staged reduction and final exit as a separate event, preserving remaining exposure and legging risk after partial execution.
12. Stop all exposure-increasing steps at the drawdown/shortfall action gates while still permitting controlled risk reduction.
13. Reproduce a historical decision without using a later corporate-action correction, adjusted series or current contract snapshot.
14. In the local screener, change a pair, strategy or parameter and obtain a distinct, replayable `V/B/X/D` chart and metrics with explicit sample, costs and proxy labels.
15. Run a small declared search, retain all candidates and show that later validation/confirmation differs from in-sample optimization; an invalid, capital-infeasible or data-incomplete candidate cannot be ranked as a winner.
16. Evaluate two candidate pairs on overlapping dates with a shared finite-capital basket ledger; prove that resource competition changes the result relative to a naive sum, and preserve the no-trade baseline.

## Deferred decisions

The following remain intentionally open until later evidence or implementation measurements:

- exact broker/account configuration and collateral treatment;
- final data source; for the private local pipeline, A-05 supplies an operating assumption on publicly accessible data, while broker/public/unattended use remains separately gated;
- final timeframe and storage engine;
- Telegram deployment details;
- final sizing formula and risk thresholds;
- BCS order semantics and automation permission;
- any later UI toolkit change beyond the delivered local standard-library S1 view; no public UI is in the current scope;
- whether regime, text or neural components add measurable value.

Stage 5 defines the module boundaries in [the architecture artifact](05-architecture.md). Stages 6–7 select conditional sources and record reference-project limits. The revised [Stage-8 plan](08-planning.md) prioritizes a visible local workbench and then guarded optimization. No live trading or broker write access is implied.
