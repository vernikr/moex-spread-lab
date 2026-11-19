# Stage 8 — Delivery and validation plan

Date: 2026-10-27

## Status and authority

Stage 8 was delivered for the offline vertical slice and is revised 2026-11-08 for the user-confirmed screener-first direction. W0–W7 mechanics and S1–S2 local screener/search slices now exist. The next separate packages are the SBRF reference reproduction and shared-capital basket evaluation/search. This plan does not create a strategy approval, broker authorization, data-use permission, deployment approval or live-trading permission.

The plan preserves the current business boundaries:

- one finite-capital virtual portfolio;
- all eligible signals attempted under a frozen priority rule, with non-execution reasons recorded;
- matched LQDT accounting using genuine external flows;
- manual VTB execution represented by delayed virtual execution;
- BCS, broker write access, automatic funding and VPS deployment disabled;
- a zero-trade or inconclusive result is a valid outcome.

## Target outcome

The S1 user-visible result answers a configurable research question:

> For a chosen futures pair, strategy and parameter set, what did one finite-capital virtual portfolio do relative to matched LQDT, how did risk and capital needs evolve, and which assumptions make that result uncertain?

The following slices must let the user search a bounded parameter space for a chosen pair and then a selected basket, showing honest later-period validation rather than only a best historical curve. The result is a private research workbench and replayable artifacts, not an order recommendation.

## First visible screener scope

S1 deliberately stays small: it reuses admitted, already ingested pairs and existing deterministic strategies, allows manual parameter edits, and renders a local dark-only view of aligned strategy/LQDT wealth, excess and risk. The user can save and compare runs. The chart labels candle-close or settlement proxies, costs, incomplete evidence and open positions. No new SBRF performance claim is required to show this first screen; the reference SBRF reproduction follows as a separate audited input.

The following pair-search slice adds a bounded, declared parameter grid, a trial journal and training plus optional final validation (revised BR-10). The basket slice then adds a shared-capital ledger before attempting joint pair/parameter selection. All slices retain W0–W7 provenance, delayed proxy execution, cost/margin stress, `V/B/X/H/D` and no-trade outcomes. Missing facts never become favorable defaults.

### Excluded from the first visible slice

- live broker connection, order submission or account polling;
- BCS adapter activation or VTB automation;
- automatic funding, core-LQDT sweeps or rescue entries;
- broad all-pairs discovery;
- ML, RL, text, regime and neural overlays;
- a public dashboard or new VPS service;
- claiming an optimized winner or a basket result before later-period and shared-capital validation;
- historical bid/ask reconstruction from OHLCV;
- current ГО or tariff values silently backfilled into expired periods;
- a claim that a free public endpoint permits unattended collection or redistribution.

## Delivered W0–W7 foundation

See [W0](../development/w0.md), the [offline pipeline and replay command](../development/w1-w7.md) and the [analytical validation](../development/w7-validation.md). Fixtures verify mechanics, not live readiness. Production-facing acceptance still requires the following evidence:

| Wave | Gate |
|---|---|
| W0 | Same fixture and manifest yield identical normalized output after restart. |
| W1 | Contract identity, price roles and source vintage are explicit; current snapshots are not historical evidence. |
| W2 | Only available-at event evidence can enter signals; price gaps cannot overwrite primary dividend dates or amounts. |
| W3 | Units, signs and integer quantities reconcile; unknown costs are not zero. |
| W4 | Revalidate both legs at execution; stale/no quotes and partial or unknown outcomes are visible. |
| W5 | Rebuild events and matched LQDT metrics after restart without double-counting cash flows or resetting risk state. |
| W6 | Every exposure increase fits the original envelope without emergency funding and passes stress; otherwise no trade. |
| W7 | No pass depends on later events, backfilled terms, fabricated fills, incomplete costs or an unresolved benchmark. The old sealed split is for W7 reproduction only. |

## Next Stage-9 delivery slices

These are new work packages within Stage 9, not a reset of W0–W7 or a new stage. Each produces a working artifact and a separate review before the next slice.

| Order | Deliverable | Acceptance evidence |
|---|---|---|
| S1 — Visible local screener (delivered 2026-11-08) | Dark-only local view backed by existing CLI/research results. At least two data-backed pair choices from the already screened universe, at least two compatible strategy choices across those pairs, editable validated parameters, run/compare, dated `V` versus `B` and `X`/drawdown charts, return/risk/capital and trade summaries. Unsupported combinations show a reason. | A changed pair, strategy or parameter produces a distinct replayable result; the same run from CLI and view agrees; no proxy chart claims a real fill or a clean account-level return. If a second pair lacks usable local inputs, intake it first rather than substituting mock numbers. |
| S2 — Table-first common-parameter laboratory (delivered 2026-11-08) | One strategy/parameter set updates all compatible pair rows with percentage-only performance and risk. Checkboxes select independent pairs; a bounded grid around the current settings logs all trials and searches training dates for a stable positive neighborhood. | No isolated peak is promoted; an inconclusive/no-plateau result is valid. Report on the optional final validation share (0–50%; at 0% report on all training data). Previously viewed history is not a prospective test. Independent pair scores are not basket wealth. See [S2](../development/screener-s2.md). |
| Execution proof (delivered) | Unified pair/calendar workbench with common-strategy search and the same starting-capital setting for each independent account. | [Proof contract and evidence](../development/execution-proof.md). |
| S3 — SBRF reference reproduction (reproduced and not accepted 2026-11-08; see [profit audit](../development/profit-audit.md)) | Admit dated near/far SBRF calendars, dividends, costs and expiry rules from source evidence. Re-run the old `calendar-spread` ideas under this project's LQDT, capital and delay accounting; keep old and new reports separate. | Explain any divergence from the reference's positive backtest, including selection, historical margin assumptions and missed signals; no historical gain is imported as current evidence. |
| S4 — Basket evaluation and search | Select a finite set of admitted pairs and compatible strategies, run concurrent proposals through one portfolio with frozen priority, shared RUB/margin/VM headroom and concentration checks; then search bounded per-pair and shared parameters on earlier periods only. | The basket `V/B/X/D` and risk path is not a sum of stand-alone curves; every blocked competing signal is logged. Selection across pairs/trials is included in the trial count and later-period checks. |
| Later | Pair dossier, broader pool ranking, persistent experiment views, monitoring, Telegram, richer optimization methods and broker adapters. | Separate evidence, resource and live-authorization gates. |

S1 and S2 are documented in the [local screener guide](../development/screener-s1.md) and [table/search record](../development/screener-s2.md). They display incomplete inputs as exploratory or blocked, never as fabricated performance or permission to trade. A checked set of independent pair studies is not the S4 shared-capital basket. SBRF reproduction remains a separate priority because the reference contains a concrete testable strategy, but its old result must pass this project's accounting independently.

## Business priority order

When resources or evidence are limited, use this order:

1. correctness of the benchmark, finite-capital and risk ledger;
2. a usable local pair/strategy/parameter comparison view on existing evidence;
3. retain honest trial history and plateau validation in the delivered common-parameter search;
4. SBRF reference-strategy reproduction and then shared-capital basket evaluation/search;
5. broader pair coverage, richer interface and signal delivery only after these foundations.

At every step, contract identity, price roles, delayed execution and cost/margin completeness are non-negotiable gates, not work to skip for a quicker chart.

The plan deliberately accepts a narrower or zero-trade result in preference to a broad but weak conclusion.

## Deferred research — LLM skills and agent setups

The landscape review [LLM skills and agent setups for futures arbitrage](../research/llm-futures-arbitrage.md) is a retained research input, not a stack selection or trading approval. Revisit it after the first deterministic baseline and the relevant fixture gates have passed.

The follow-up should evaluate only potentially useful, reproducible components:

- custom skills for provenance, contract lifecycle, spread statistics, execution stress, risk gates and post-trade review;
- read-only MCP tools with typed outputs and explicit source/freshness fields;
- Qlib/RD-Agent-style hypothesis and validation loops;
- structured role, memory and adversarial-review patterns from financial-agent projects; and
- measured model cost, latency, repeatability and incremental value against the deterministic baseline.

The LLM layer must remain outside numeric truth, sizing, margin, order state and execution authorization. A failed or inconclusive comparison is a valid reason to defer it again.

## Acceptance gates for Stage 9 development

Existing fixture-level gates remain in force; each new slice must demonstrate its applicable gates before making the corresponding result claim:

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
| G13 Screener consistency | Selected pair/strategy/parameters reproduce the same dated wealth/risk series via CLI and local view; unsupported combinations explain why | Block visible result |
| G14 Search integrity | Bounded trial registry; train-only proposals, rank and neighborhood checks; report on validation (or training at 0%); preserve dates, share and cost sensitivity; label explored history | No optimized-winner or prospective-performance claim |
| G15 Basket integrity | Concurrent signals share one finite-capital ledger, matched LQDT flows and trial history | No basket performance claim |

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

The user has already prioritized the screener and pair/basket parameter search; no new business decision is required to start S1. Stronger claims or live use may later require:

- a redacted LQDT price/statement fixture for account-level benchmark reconciliation;
- applicable VTB tariff and account/collateral evidence;
- source evidence for SBRF historical contracts, dividends and execution costs; the prior project's results are a hypothesis to reproduce, not accepted data;
- a later choice about higher-quality executable data if the user wants claims beyond the accepted private candle-proxy research boundary; A-05/A-08 do not block the local screener;
- operational response and availability observations for a future shadow workflow.

These inputs are gates for stronger conclusions, not prerequisites for building the offline schemas and replay fixtures.

## Definition of done for Stage 8

Stage 8 remains complete as a stage artifact. This revision defines S1–S4 as the next separate Stage-9 work packages with user-visible acceptance outcomes. They do not include broker access or deployment.
