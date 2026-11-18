# Stage 5 — Architecture

Date: 2026-10-26

## Status and authority

Stage-5 architecture is delivered and revised 2026-11-08 for a screener-first research workflow. It turns the functional requirements into boundaries, state transitions and decision records. A local interface and search coordinator are now early consumers of the existing research core; no broker integration or live trading is authorized.

The architecture must support a deterministic strategy that can enter, add, reduce and exit in stages without turning a single persistent deviation into unbounded averaging down.

## Architectural principles

1. **Research and execution share the same domain rules.** Backtests, delayed virtual execution and future broker adapters consume the same strategy specification and risk gates.
2. **Events are append-only.** Corrections and unknown outcomes are new events; prior observations are not silently overwritten.
3. **Exposure changes are explicit.** An initial entry, scale-in, staged reduction and final exit are separate decisions and separate portfolio events.
4. **Every increase fails closed.** Missing data, unknown costs, unavailable capital, stale safety state, failed hedge or unresolved reconciliation blocks exposure-increasing actions.
5. **The benchmark is a parallel accounting view.** LQDT comparison is reconciled from the same genuine external flows and does not share the strategy's pilot-induced transfers.
6. **Heavy work stays on the laptop.** The future VPS runtime is a bounded monitor, virtual-ledger/outbox worker and alert process; it is not the research warehouse or public application host.

## Modularity assessment

The previous proposal had the right conceptual seams, especially replaceable source adapters, strategy specifications, broker boundaries and notification adapters. It was not yet sufficient for independent teams: it did not define module contracts, capability discovery, version compatibility, ownership, activation/rollback or protection from direct shared-state coupling.

This revision makes the architecture **contract-first and modular by default**. It does not require microservices. The first implementation may use one process and one repository, provided the same public contracts, package boundaries, fixtures and contract tests are enforced. A module may later move to another process or repository without changing the domain model.

### Module contract

Every replaceable module must publish a manifest containing:

| Field | Purpose |
|---|---|
| `module_id` and SemVer | Stable identity and release compatibility |
| `contract_version` | Version of the input/output/event contract |
| `kind` | `data`, `specification`, `tariff`, `strategy`, `risk`, `execution`, `accounting`, `report`, `notification` or `artifact` |
| capabilities | Explicit operations and fields supported; no implied behavior |
| inputs/outputs | Typed schemas, timestamps, units and missingness semantics |
| dependencies | Required contract versions and optional capabilities |
| owner | Team/contact and escalation path, without embedding credentials |
| resource profile | CPU, memory, disk, network and offline/online requirements |
| rights/provenance | Data-use restrictions, source lineage and retention limits |
| determinism | Whether identical inputs must produce identical outputs and how randomness is seeded |
| health/compatibility | Self-check, supported runtime and migration status |

The registry stores manifests and immutable release references. It does not grant a module permission to bypass risk gates or write directly to the portfolio.

Non-executable artifacts are first-class versioned inputs: model parameters, tariff schedules, contract tables, risk policies, source vintages, fixtures and report definitions. Each artifact has an ID, schema version, content hash, effective/publication/receipt dates, rights status and producing module. A run pins artifact references alongside executable module releases. An unknown or incompatible artifact blocks only the dependent operation.

### Replaceable module map

| Module | Owns | May depend on | Must not own |
|---|---|---|---|
| Data provider | Retrieval, raw capture, timestamps, normalization and quality status | Source credentials through secure runtime only | Strategy logic, sizing or orders |
| Contract/specification provider | Contract, calendar, lot, tick, expiry and margin facts | Dated source evidence | Signal decisions or account state |
| Tariff/cost provider | Effective-dated commissions, exchange/clearing charges, funding and scenario costs | Contract, broker and account profile inputs | Market observations, positions or fills |
| Strategy module | Candidate relationships, parameter schema/defaults, indicators, ladder and target-exposure proposal | Normalized point-in-time data and declared specifications | Credentials, tariff lookup internals, order submission or portfolio mutation |
| Risk module | Feasibility and increase/reduction decision with reason codes | Portfolio snapshot, cost result, limits and stress inputs | Strategy discovery, broker calls or hidden funding |
| Execution adapter | Translation of order intents and lifecycle observations | Public order schema and broker session | Strategy/risk decisions, portfolio policy or silent fills |
| Portfolio/accounting core | Event log, projections, reservations, fills, benchmark and reconciliation | Validated domain events | Provider-specific parsing or team-owned strategy code |
| Artifact registry | Immutable artifact references, schemas, hashes, dates and rights status | Producing modules and approved storage | Runtime decisions, silent replacement or private credentials |
| Report module | Reproducible charts, metrics, comparisons and limitations | Versioned projections and run manifest | New trading decisions or event mutation |
| Search coordinator | Bounded parameter trials, chronological selection and experiment history for a pair or basket | Strategy parameter schemas, portfolio evaluator, risk and validation contracts | Strategy-specific math, broker actions or rewritten trial results |
| Local screener | Pair/strategy controls, run comparison and explicit evidence status | Report, registry and search read contracts | Independent P&L calculations, portfolio writes or broker access |
| Notification adapter | Delivery and retry status | Durable outbox contract | Authorization, risk changes or event creation |

The portfolio/accounting core remains a protected domain boundary rather than an interchangeable plugin. Its projections expose read contracts; external modules submit commands that become validated events.

### Independent-team rules

- A team owns a module directory/package, manifest, schemas, fixtures, contract tests and changelog.
- Cross-module imports are allowed only through published ports/contracts. No module reads another module's private database tables, files or in-memory objects.
- Shared events and schemas are versioned artifacts. Additive changes are preferred; breaking changes require a new contract version and an explicit migration/compatibility window.
- A module is accepted through conformance tests against golden fixtures, including missing data, stale data, unknown values and failure responses. Passing a unit test against private implementation details is insufficient.
- A run pins the complete module manifest set. Replacing a strategy, tariff schedule or data provider creates a new run configuration and never changes an existing result.
- A module can be disabled without deleting its historical artifacts. If a required capability is unavailable, the dependent signal is rejected with a stable reason code.
- Credentials and private account data are injected only at the adapter boundary. A strategy or tariff module receives typed account/cost inputs, never raw credentials or unrelated account records.

### Activation and rollback

The run manifest selects modules by `module_id`, release, contract version and configuration hash. A module registry must support:

1. discovery and compatibility checking before a run;
2. explicit enable/disable and feature flags at run or environment scope;
3. dry-run validation against fixtures;
4. atomic activation of a compatible release;
5. rollback to the previous release without rewriting events;
6. a clear `module_unavailable`, `contract_incompatible` or `artifact_revoked` outcome.

Disabling a strategy stops new proposals from that module but does not cancel, alter or hide existing portfolio positions. Disabling a tariff or data provider blocks dependent new exposure if no approved compatible replacement is available.

## System boundaries

```text
source files/APIs
       |
       v
point-in-time intake ---> raw evidence and specifications
       |
       v
normalized observations ---> strategy evaluator ---> signal/step proposals
                                      |                         |
                                      v                         v
                              trial registry             risk and feasibility gate
                                                                |
                                     +--------------------------+------------------+
                                     |                                             |
                                     v                                             v
                            virtual portfolio event log                    durable outbox
                                     |                                             |
                         +-----------+-----------+                             v
                         |                       |                       notification adapter
                         v                       v
                 portfolio projection     LQDT benchmark projection
                         |                       |
                         +-----------+-----------+
                                     v
                              reports and audit
```

### 1. Point-in-time intake

Owns source adapters, raw references/hashes where retention is permitted, normalization, quality status and the three timestamps: exchange event, source publication/update and local receipt. It keeps price roles separate (`settlement`, `legal_close`, `last_trade_close`, `bid`, `ask`, `live_mark`) and stores corporate actions as source observations plus explicitly derived checks. It does not decide whether a trade is attractive.

### 2. Specification registry

Versions contract terms, lot size, tick value, expiry, calendar, session, margin/risk inputs, fees, data-source terms, module manifests, strategy specifications and adjustment modes. Every evaluation records the complete run manifest, specification versions, consumed price roles and corporate-action vintages used. Tariffs are providers of dated cost facts, not constants embedded in strategies.

### 3. Strategy evaluator

Runs no-trade and deterministic baselines first. It generates proposals from available observations only. It may calculate a desired target exposure, but it cannot mutate portfolio state or bypass the risk gate.

### 4. Signal and step engine

Creates stable signal identities, applies delayed manual revalidation and converts a strategy's target exposure into an explicit step proposal. It records why a step was proposed, accepted, rejected, expired or superseded.

### 5. Risk and feasibility gate

Checks both legs, session, quote age, data quality, costs, integer quantities, available cash, margin, VM liquidity, reserve, concurrent commitments, correlation, stressed loss through reaction/exit and benchmark-relative limits. It has separate paths for exposure increase and risk reduction. A top-up is never an input that makes an increase pass.

### 6. Virtual portfolio event log and projections

The single virtual portfolio is the source of truth for positions, reservations, orders, fills, funding and reconciliation. Read models for current exposure, cash, risk path and reports are rebuildable from the event log.

### 7. LQDT benchmark and attribution

Maintains the matched no-pilot path and calculates `V`, `B`, `X`, `H`, `D`, shortfall, costs and funding effects without resetting peaks or lifetime usage.

### 8. Outbox and notifications

Writes an idempotent durable notification record before delivery. Delivery failure cannot authorize, repeat or alter a portfolio step.

### 9. Module registry and contract validation

Discovers manifests, checks contract compatibility, validates required capabilities and pins the module set for each run. It performs no trading decision. Registry failure blocks activation of dependent modules and leaves prior projections/events intact.

## Screener and search boundary

The local screener is a read-only presentation and run-request layer over the same pinned research engine used by CLI runs. Its primary view is a pair table: one strategy, parameter set and cost scenario are applied to every compatible pair, with explicit unavailable/blocked/short-history outcomes. Row checkboxes select independent pairs for common-parameter search; a secondary detail uses indexed `V/B` and percentage-excess charts. Absolute RUB amounts remain in the protected ledger for policy checks, not in the interface. Changing a control creates a new immutable experiment; rendering never changes portfolio history.

Strategy modules publish parameter schemas (type, unit, range, dependencies, default and search eligibility). The S2 search coordinator tests a bounded neighborhood of one common parameter set on checked pairs, using each pair's independent evaluator and common dates. It records every trial and rejects capital-infeasible or unavailable inputs. Training alone selects a stable positive region under an explicit neighboring-parameter rule. `EvaluationSplit` supplies the training prefix and optional validation suffix from one persistent project setting (0–50%, default 0). The current/chosen rule is reported on validation if present, otherwise on training. The chart and ledger stay continuous. The history has already been explored, so that diagnostic is not a sealed prospective test. A score is not an execution or profit guarantee. S4 later replaces independent scoring with a shared-capital basket evaluator before making any basket-wealth claim.

A pair result may use a one-pair projection. A basket result must replay concurrent signals in **one** shared-capital virtual portfolio with a frozen priority/selection rule, integer lots, correlated exposure and continuous matched LQDT flows. Summing independently optimized pair curves is never a basket backtest. A standalone pair run and a basket run are different experiment types and cannot be blended into one wealth curve. The private interface and report layer must not turn candle-close proxies, a reference SBRF result or a provisional account screen into live-trading approval.

## Strategy specification: exposure ladder

The deterministic strategy contract must contain an immutable, versioned ladder. Numeric thresholds remain research parameters until validated.

| Field | Meaning |
|---|---|
| `ladder_id` / version | Identity of the tested rule set |
| direction and relationship | Signed spread definition and economic pair family |
| trigger state | Point-in-time condition for entering or changing target exposure |
| steps | Ordered levels, each with threshold, target quantity and maximum waiting age |
| aggregate cap | Maximum contracts, margin, stress loss and capital use for the whole idea |
| reduction ladder | Target-based partial reductions and final-exit conditions |
| invalidation | Structural break, expiry, stale data, session, cost, liquidity and hedge conditions |
| cooldown/deduplication | Prevents repeated events from one unchanged observation |
| risk overrides | Conditions that force reduction or block all increases |

Example shape, without prescribing values:

```text
FLAT
  -> ENTER target level 1 when deviation >= e1 and all gates pass
  -> ADD level 2 when deviation >= e2, fresh revalidation passes, and total cap passes
  -> ADD level 3 when deviation >= e3, fresh revalidation passes, and total cap passes
  -> REDUCE level 2 at target r1 or risk override
  -> REDUCE level 1 at target r2 or risk override
  -> EXIT at target r3, invalidation, expiry or controlled risk exit
```

The ladder is not a license to add because the position is losing. The evaluator must compare continuation from the current state against remaining costs, LQDT opportunity cost, time-to-exit and risk. A step that fails any gate becomes a recorded no-add outcome; it does not silently resize to an affordable amount unless the strategy specification explicitly allows a smaller integer step.

## Portfolio state machine

The portfolio tracks target exposure and actually filled exposure separately for each leg.

```text
FLAT
  -> STEP_PROPOSED
  -> STEP_REVALIDATED
  -> STEP_ACCEPTED
  -> LEG_WORKING
  -> PARTIALLY_FILLED / HEDGE_REQUIRED
  -> TARGET_REACHED
  -> REDUCTION_PROPOSED
  -> FLAT or a lower TARGET_LEVEL
```

Any state may transition to `REJECTED`, `EXPIRED`, `UNKNOWN_OUTCOME`, `RISK_REDUCTION_REQUIRED` or `RECONCILIATION_REQUIRED`. The state machine never assumes that both legs filled together.

## Required portfolio events

Events are immutable and carry `portfolio_id`, `idea_id`, `ladder_id`, `step_id`, strategy/specification versions, event time, receipt time, source-vintage references and causal parent IDs where applicable.

### Observation and decision events

- `market_observation_recorded`
- `corporate_action_observed` / `corporate_action_corrected`
- `derived_price_check_recorded`
- `strategy_evaluation_recorded`
- `signal_created`
- `signal_revalidated`
- `step_proposed` — initial entry, scale-in, staged reduction or final exit
- `step_rejected` — with a stable rejection reason and relevant measured values
- `step_expired` / `step_invalidated`

### Feasibility and risk events

- `exposure_increase_check_passed` or `exposure_increase_blocked`
- `risk_state_changed` — warning, action, outer-boundary or data/broker incident
- `capital_reserved` / `capital_released`
- `funding_authorized` / `funding_credited` / `funding_rejected`
- `risk_reduction_required`

### Two-leg execution events

- `leg_order_intent_created`
- `leg_order_acknowledged`
- `leg_partially_filled` / `leg_filled`
- `leg_cancel_requested` / `leg_cancelled`
- `leg_outcome_unknown`
- `hedge_required`
- `hedge_completed` or `position_flattened`

### Valuation and recovery events

- `position_marked`
- `variation_margin_recorded`
- `fee_recorded`
- `benchmark_flow_recorded`
- `portfolio_reconciled`
- `reconciliation_exception`
- `notification_enqueued` / `notification_delivered` / `notification_failed`

### Module and artifact lifecycle events

- `run_manifest_pinned`
- `module_manifest_registered`
- `module_compatibility_checked`
- `module_activated` / `module_disabled`
- `module_rolled_back`
- `module_unavailable` / `contract_incompatible` / `artifact_revoked`

An event must not carry secret credentials, account identifiers or raw private statements. Private source material is referenced by a local or secure-runtime identifier and content hash where permitted.

## Scale-in and staged-exit rules

1. A scale-in is an `exposure_increase` event, even when it belongs to an already open idea.
2. Each scale-in starts a new decision lifecycle: fresh data vintage, current prices for both legs, current costs, current margin/VM path and current risk state.
3. The requested quantity is the next declared ladder step, rounded to whole contracts. The engine does not invent a replacement quantity after rejection.
4. The aggregate idea cap and portfolio-wide cap apply to the sum of all filled steps, not only the latest step.
5. `D >= 10,000` or `X <= -10,000` blocks scale-ins and other exposure increases. Earlier safety failures override the numbers.
6. Emergency funding cannot make a scale-in pass. A top-up may only support orderly de-risking of an existing position under the policy.
7. A staged exit reduces target exposure; every leg is tracked independently. A partial fill leaves an explicit residual target and legging-risk state.
8. Target-based exits, invalidation exits and risk-forced exits are separate reasons in the event log and report.
9. A reduction is not allowed to reset the idea's ladder, loss history, peak, funding use or benchmark accounting.
10. Repeated observations at one threshold are deduplicated. A new step requires a new crossing or an explicitly declared revalidation condition.

## Decision records

### ADR-05-01 — One append-only event log with rebuildable projections

**Decision:** Keep the virtual portfolio and operational history as immutable domain events; derive current state and reports as projections.

**Reason:** Restart recovery, auditability, partial fills, corrections and exact replay are more important than premature storage optimization.

**Consequence:** The first implementation needs event schemas and idempotent projection logic before a UI or broker adapter.

### ADR-05-02 — Exposure ladders are strategy data, not portfolio improvisation

**Decision:** Scale-in/out thresholds, quantities, caps and invalidations are versioned strategy configuration and are evaluated identically in research and virtual execution.

**Reason:** This makes flexible sizing testable and prevents a losing position from acquiring discretionary, unrecorded additions.

**Consequence:** Each tested ladder is a trial-registry item and must be compared with fixed-size and capped-adaptive baselines.

### ADR-05-03 — Separate increase and reduction gates

**Decision:** Use a strict gate for exposure increases and a safety-prioritized path for reductions/flattening.

**Reason:** A position may need to be reduced when data or capital is unsafe even though opening a new position is prohibited.

**Consequence:** Risk incidents must be able to produce reduction events without reopening entry authority.

### ADR-05-04 — Delayed manual execution is a first-class mode

**Decision:** Model notification, reading, decision, order and fill delays before any future automation mode.

**Reason:** The initial workflow is manual through VTB and a signal that only works at its creation timestamp is not a usable signal.

**Consequence:** Every step has creation and revalidation timestamps and can expire without a fill.

### ADR-05-05 — Laptop-first research, bounded optional VPS runtime

**Decision:** Separate research/ETL/report generation from the future lightweight monitor, ledger and notification worker.

**Reason:** The VPS has shared memory and disk headroom and must preserve existing VPN services.

**Consequence:** No new public listener, heavy archive or unrestricted parameter search is part of the first deployment boundary.

### ADR-05-06 — Contract-first modularity without premature microservices

**Decision:** Define every replaceable strategy, data source, tariff, risk profile, execution mode, report and notification channel through a manifest, typed port and conformance fixtures. Keep the initial deployment free to run in one process.

**Reason:** Independent teams need stable ownership and compatibility rules, while microservices would add operational cost and failure modes before the workload justifies them.

**Consequence:** Package boundaries and contract tests are mandatory even when implementation modules share a process. A later split is an optimization, not a redesign of domain behavior.

### ADR-05-07 — No direct shared-state coupling

**Decision:** Modules exchange commands, typed results and immutable events. They cannot read or write another module's private tables, files or memory state.

**Reason:** Direct shared state makes strategies, tariffs and providers impossible to disable, replace or develop independently and makes historical replay ambiguous.

**Consequence:** The portfolio core owns mutations; modules expose ports and read models. Schema changes require compatibility checks and explicit migrations.

### ADR-05-08 — Version-pinned run manifests

**Decision:** Every research, virtual-execution and future live-mode run pins the complete set of module releases, contract versions, configurations, source vintages and artifact hashes.

**Reason:** A strategy result must remain reproducible when a tariff schedule, source adapter or risk policy changes.

**Consequence:** Disabling or rolling back a module affects future runs and operation, never historical events or reports.

### ADR-05-09 — Capability-based safe degradation

**Decision:** Modules declare capabilities and the engine permits only operations supported by the complete active set. Missing or incompatible capabilities produce explicit blocked outcomes.

**Reason:** New or unexpected artifacts must not silently become zero cost, unlimited capacity or an unvalidated fallback.

**Consequence:** A disabled strategy stops proposals; a disabled cost/data/risk capability blocks dependent exposure; reporting can continue from already recorded events.

### ADR-05-10 — Price roles and corporate actions are explicit

**Decision:** Settlement, analytical close, last-trade close, quotes and live marks are separate contract fields. Corporate actions retain primary dates and availability vintages; price-gap and adjustment calculations are derived diagnostics.

**Reason:** The previous projects show that a generic `close`, current catalog snapshot or inferred `registry_date - 2 days` can silently mix accounting, research and execution meanings or leak later information into history.

**Consequence:** Each consumer declares its price role and adjustment mode. Missing role or unavailable event vintage blocks the dependent historical claim rather than selecting a convenient fallback.

### ADR-05-11 — Research screener before signal delivery

**Decision:** A local dark-only screener consumes versioned pair, strategy, search and report contracts. Automatic parameter selection remains a separate, bounded research operation; a chart or selected configuration never creates an order intent.

**Consequence:** The first Stage-9 user-visible slice reuses the existing analytical runs and LQDT ledger. A later basket slice must add shared-capital portfolio evaluation before basket optimization; the event, risk and evidence boundaries remain intact.

## Stage-5 acceptance gates

Architecture is ready for stack selection when a small fixture can:

1. Replay an entry, two scale-ins, two staged reductions and a final exit deterministically.
2. Reject the second scale-in because of capital, stale data, a risk trigger or an aggregate cap and preserve the rejection reason.
3. Show that a partial fill in one leg produces `hedge_required` and a residual position, not a balanced phantom position.
4. Rebuild the same exposure, `X`, `D` and funding history after restart without duplicate events.
5. Demonstrate that emergency funding supports only risk reduction and cannot authorize a new ladder step.
6. Keep strategy events, benchmark flows and private credentials separated in the resulting report.
7. Replace a strategy and a tariff module using only a new run manifest, without changing portfolio-core code or historical results.
8. Disable a strategy and prove that it creates no new proposals while existing positions and events remain visible.
9. Reject a module with an incompatible contract or missing capability before activation and produce a stable reason code.
10. Run a module's conformance fixtures without credentials, private account data or access to another module's private state.

## Open inputs for implementation after Stage 7

- broker-specific margin offsets, liquidation and order semantics;
- applicable VTB tariff and permitted manual data access;
- BCS API permissions and future automation semantics;
- executable quote/depth availability and retention rights;
- final storage and deployment choice after measuring the first offline slice;
- validated ladder thresholds, quantities, stress multipliers and risk limits.

Stage 6 selected the conditional baseline in [the stack and data-source artifact](06-stack-and-data.md) within these boundaries. Stage 7 revised the price/event provenance boundary in [the reuse analysis](07-reuse-analysis.md). Broker evidence and implementation measurements may revise this architecture through explicit decision records.
