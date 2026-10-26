# Stage 8 — Delivery and validation plan

Date: 2026-10-27

## Status and authority

Stage 8 is delivered as the implementation plan for the first offline vertical slice. It sequences work and defines acceptance gates; it does not create a strategy approval, broker authorization, data-use permission, deployment approval or live-trading permission.

The plan preserves the current business boundaries:

- one finite-capital virtual portfolio;
- all eligible signals attempted under a frozen priority rule, with non-execution reasons recorded;
- matched LQDT accounting using genuine external flows;
- manual VTB execution represented by delayed virtual execution;
- BCS, broker write access, automatic funding and VPS deployment disabled;
- a zero-trade or inconclusive result is a valid outcome.

## Target outcome

The first implementation must answer one narrow question reproducibly:

> Given one admitted MOEX futures-spread candidate, dated specifications and costs, conservative delayed execution assumptions and a matched LQDT path, does the idea produce an economically meaningful and executable result, or should it be rejected as unknown, infeasible or unprofitable?

The result is a research report and replayable artifacts, not an order recommendation.

## First-slice scope

### Included

1. A no-trade `H0` LQDT control.
2. One candidate calendar/carry family, with CNY/RUB as the default candidate from Stage 6, subject to contract and data gates.
3. One simple deterministic spread baseline and one economic carry baseline where the required inputs exist.
4. Daily data as the primary resolution; finer data only for a declared delayed-execution sensitivity.
5. Versioned contract/specification snapshots, costs, source rights, corporate-action inputs and price roles.
6. One event-sourced virtual portfolio with staged exposure events, partial legs and rejection reasons.
7. Delayed manual revalidation scenarios covering the declared 3–90 minute response range.
8. `V`, `B`, `X`, `H`, `D`, costs, margin/VM liquidity and lifetime funding usage.
9. Reproducible reports, fixtures, hashes and contract/conformance checks.

### Excluded from the first slice

- live broker connection, order submission or account polling;
- BCS adapter activation or VTB automation;
- automatic funding, core-LQDT sweeps or rescue entries;
- broad all-pairs discovery;
- ML, RL, text, regime and neural overlays;
- a public dashboard or new VPS service;
- historical bid/ask reconstruction from OHLCV;
- current ГО or tariff values silently backfilled into expired periods;
- a claim that a free public endpoint permits unattended collection or redistribution.

## Delivery sequence

Each work package has a clear input/output contract and can be owned by an independent team. A package is not accepted through private implementation tests alone: it must pass the shared schema fixtures and produce provenance.

### W0 — Run and evidence manifest

**Purpose:** make every later result reproducible before adding market logic.

**Deliverables:**

- pinned module/artifact manifest;
- source request manifest and rights-status field;
- run ID, configuration hash and trial counter;
- fixture loader that works without network access;
- redaction and secret-scanning check.

**Gate:** the same fixture and manifest produce the same normalized output after a clean restart.

### W1 — Catalog and price-role intake

**Purpose:** build the smallest trustworthy input set.

**Deliverables:**

- dynamic ISS adapter pattern: board discovery, aliases, pagination and explicit fields;
- raw response retention where permitted, with content hash and receipt time;
- stable contract identity, expiry and source-vintage checks;
- separate settlement, last-trade close, analytical close, bid/ask and live-mark fields;
- candidate-pair registry and an explicit “not admitted” outcome.

**Gate:** no reused short code, missing required field or current snapshot is accepted as historical evidence without an explicit status.

### W2 — Corporate-action and dividend evidence

**Purpose:** prevent dividend and split information from leaking into historical decisions.

**Deliverables:**

- official-first corporate-action adapter;
- source roles, availability vintages and conflict status;
- reviewed seed fixture and conflicting-source fixture;
- split/adjustment lineage;
- derived price-gap check kept separate from factual event dates.

**Gate:** an event without reliable `available_at` cannot affect a historical signal. A price gap cannot overwrite the primary ex-date or amount.

### W3 — Domain normalization and spread economics

**Purpose:** turn source observations into typed, unit-safe domain inputs.

**Deliverables:**

- contract terms, lot, tick, multiplier, expiry and settlement type;
- calendar-spread direction and leg mapping;
- raw and derived spread series;
- carry inputs, dividend window treatment and reference-rate inputs where applicable;
- explicit missingness and quality statuses;
- cost profiles with separate exchange, clearing, broker, spread/slippage, funding and exit components.

**Gate:** unit checks, sign checks, integer quantities and missing-cost failures pass. Unknown cost is never treated as zero.

### W4 — Delayed virtual execution

**Purpose:** model the user's manual workflow rather than filling at signal time.

**Deliverables:**

- signal timestamp and simulated decision timestamp;
- fresh revalidation of both legs, session, quote age, costs, capital and margin;
- native calendar-spread scenario and independent-two-leg scenario where both are relevant;
- conservative no-quote, stale, partial-fill, one-leg and unknown-outcome paths;
- explicit exposure-increase, reduction and exit lifecycles.

**Gate:** no candle midpoint or stale signal becomes a fill. Partial execution never becomes an invisible balanced position.

### W5 — Portfolio and matched LQDT accounting

**Purpose:** produce the single realistic portfolio and benchmark-relative ledger.

**Deliverables:**

- append-only portfolio events and rebuildable projections;
- cash, reservations, positions, VM, costs, margin needs and funding events;
- matched `B(t)` path and strategy-enabled `V(t)` path;
- continuous `X`, `H`, `D`, cumulative shortfall and lifetime top-up usage;
- no double-counting checks for internal transfers, LQDT return and futures P&L;
- explicit unresolved attribution state.

**Gate:** replay after restart produces identical events, projections and benchmark-relative metrics. Missing actual LQDT inputs block a clean account-level conclusion.

### W6 — Baseline strategies and exposure ladder

**Purpose:** test the economic idea before adding model complexity.

**Deliverables:**

- no-trade control;
- fixed-parameter spread mean-reversion control;
- carry/calendar hypothesis only when its reference rate, contract terms and dividend treatment are valid;
- predeclared entry, add, reduce and exit ladder;
- fixed quantities, aggregate cap, invalidation, cooldown and risk overrides;
- complete trial registry, including rejected signals and parameter attempts.

**Gate:** every increase passes the original RUB 30,000 envelope without emergency funding and the stress condition from the pilot policy. A failing minimum step means no trade or a revised, versioned hypothesis—not an invented smaller fill.

### W7 — Validation and report

**Purpose:** distinguish a useful result from an attractive historical artifact.

**Deliverables:**

- chronological train/test or walk-forward split;
- sealed confirmation period;
- delay, cost and mark sensitivity;
- null/sign-flip and synthetic-no-edge checks;
- regime and contract-vintage breakdown where sample size permits;
- result decomposition: gross movement, costs, funding, execution loss, LQDT opportunity cost and residual uncertainty;
- list of all eligible, rejected, stale, unfilled and partially filled opportunities.

**Gate:** no PASS conclusion if the result depends on a later event, a current specification, fabricated execution, incomplete costs or an unresolved benchmark path.

## Business priority order

When resources or evidence are limited, use this order:

1. correctness of the benchmark and risk ledger;
2. correctness of contract identity, settlement and price roles;
3. honest delayed execution and non-execution recording;
4. cost and margin completeness;
5. one understandable economic hypothesis;
6. breadth of pairs and interface convenience.

The plan deliberately accepts a narrower or zero-trade result in preference to a broad but weak conclusion.

## Acceptance gates for Stage 9 development

Development may begin only after the following fixture-level gates are demonstrated:

| Gate | Evidence required | Failure outcome |
|---|---|---|
| G1 Rebuild | Clean offline replay matches hashes and outputs | Block release |
| G2 Provenance | Source, request, receipt, publication/update and event times are traceable | Block historical claim |
| G3 Price roles | Settlement, close, quote and live mark cannot collide | Block accounting or execution path |
| G4 Contract identity | Expiry, stable ID, lot, tick and units are validated | Reject affected contract/pair |
| G5 Corporate actions | Availability vintage, source role and conflicts are visible | Block affected historical signal |
| G6 Costs | Both legs/scenarios and unknown components are explicit | Block net-advantage claim and increases |
| G7 Execution | Delayed, stale, partial and one-leg paths replay correctly | Block fill-based result |
| G8 Portfolio | Event replay preserves positions, cash, `X`, `D` and funding history | Block report |
| G9 Risk | Ladder, integer size, stress and funding rules fail closed | Reject step or strategy |
| G10 Validation | Baselines, holdout, null checks and sensitivity are reported | Mark inconclusive/reject |
| G11 Modularity | A provider, tariff and strategy can be replaced by manifest | Block module acceptance |
| G12 Security | No credentials, account identifiers or private statements in artifacts | Block commit/release |

## Release states

The first software artifacts use explicit states:

- **Fixture-only:** schemas and replay work; no external data conclusion.
- **Research-ready:** source vintages, price roles, costs and benchmark fixture pass G1–G6.
- **Exploratory result:** a baseline has been replayed, but execution or account evidence remains incomplete.
- **Inconclusive:** a required source, event, cost, quote or benchmark input is missing.
- **Rejected:** the candidate fails economic, capital, validation or risk gates.
- **Live-blocked:** any research result may exist, but broker/account/data-rights prerequisites are not satisfied.

No state in this list authorizes funding, orders or deployment.

## Required inputs and decisions

No new business decision is required to start W0–W3. Later gates may require the user to provide or approve:

- a redacted LQDT price/statement fixture for account-level benchmark reconciliation;
- applicable VTB tariff and account/collateral evidence;
- a choice between a narrower research universe and an authorized higher-quality executable-data source if free-data rights remain unresolved;
- operational response and availability observations for a future shadow workflow.

These inputs are gates for stronger conclusions, not prerequisites for building the offline schemas and replay fixtures.

## Definition of done for Stage 8

Stage 8 is complete when an independent team can read this plan, obtain the public schemas/fixtures, implement one work package without credentials, and know exactly which result is accepted, blocked or inconclusive. Stage 9 begins with W0 and does not include broker access or deployment.
