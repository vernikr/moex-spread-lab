# Stage 5 — Architecture

Date: 2026-10-26

## Status and authority

Stage-5 architecture is delivered. It turns the functional requirements into boundaries, state transitions and decision records. It does not select a final programming language, database, market-data vendor or broker integration. It does not authorize live trading.

The architecture must support a deterministic strategy that can enter, add, reduce and exit in stages without turning a single persistent deviation into unbounded averaging down.

## Architectural principles

1. **Research and execution share the same domain rules.** Backtests, delayed virtual execution and future broker adapters consume the same strategy specification and risk gates.
2. **Events are append-only.** Corrections and unknown outcomes are new events; prior observations are not silently overwritten.
3. **Exposure changes are explicit.** An initial entry, scale-in, staged reduction and final exit are separate decisions and separate portfolio events.
4. **Every increase fails closed.** Missing data, unknown costs, unavailable capital, stale safety state, failed hedge or unresolved reconciliation blocks exposure-increasing actions.
5. **The benchmark is a parallel accounting view.** LQDT comparison is reconciled from the same genuine external flows and does not share the strategy's pilot-induced transfers.
6. **Heavy work stays on the laptop.** The future VPS runtime is a bounded monitor, virtual-ledger/outbox worker and alert process; it is not the research warehouse or public application host.

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

Owns source adapters, raw references/hashes where retention is permitted, normalization, quality status and the three timestamps: exchange event, source publication/update and local receipt. It does not decide whether a trade is attractive.

### 2. Specification registry

Versions contract terms, lot size, tick value, expiry, calendar, session, margin/risk inputs, fees, data-source terms and strategy specifications. Every evaluation records the specification versions used.

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

## Stage-5 acceptance gates

Architecture is ready for stack selection when a small fixture can:

1. Replay an entry, two scale-ins, two staged reductions and a final exit deterministically.
2. Reject the second scale-in because of capital, stale data, a risk trigger or an aggregate cap and preserve the rejection reason.
3. Show that a partial fill in one leg produces `hedge_required` and a residual position, not a balanced phantom position.
4. Rebuild the same exposure, `X`, `D` and funding history after restart without duplicate events.
5. Demonstrate that emergency funding supports only risk reduction and cannot authorize a new ladder step.
6. Keep strategy events, benchmark flows and private credentials separated in the resulting report.

## Open inputs for Stage 6

- broker-specific margin offsets, liquidation and order semantics;
- applicable VTB tariff and permitted manual data access;
- BCS API permissions and future automation semantics;
- executable quote/depth availability and retention rights;
- final storage and deployment choice after measuring the first offline slice;
- validated ladder thresholds, quantities, stress multipliers and risk limits.

Stage 6 may select technologies only within these boundaries. Research findings, broker evidence and the later reuse stage may revise this architecture through explicit decision records.
