# Stage 7 — Reuse analysis of previous MOEX projects

Date: 2026-10-27

## Status and authority

Stage 7 is delivered. This document records a reuse assessment of three repositories and the supplied product-description compilation. It separates implemented code and stored data from research hypotheses, UI mock data and dated observations.

The assessment changes the Stage-6 baseline in the areas of price roles, ISS ingestion, dividend/corporate-action provenance and historical LQDT extensions. It does not approve a broker connection, data-rights interpretation, live trading or a strategy.

## Materials inspected

| Material | Revision inspected | Nature | Reuse status |
|---|---|---|---|
| [`calendar-spread`](https://github.com/vernikr/calendar-spread) | `39e18da1baf9b056ce6699669a9afffaf783b51b` | Working educational Python pipeline, MIT-licensed | Code ideas and local artifacts are reusable after contract adaptation |
| [`moex-tool`](https://github.com/vernikr/moex-tool) | `6eb2d4e4a6c719e4da5da3de24b459f4bf3eb28f` | Working local Python tool with tests | Strongest reusable data-ingestion and dividend-pipeline ideas; not copied as a whole |
| [`futures-screener-ui-2`](https://github.com/vernikr/futures-screener-ui-2) | `f8955e245f31bc73621878ec801537c4ff22824b` | UX prototype with deterministic mock data | UX and schema ideas only; mock market values are not evidence |
| `СКРИНЕР_epub.md` | Copied unchanged; SHA-256 `204b2cd861bb68004f067399102b29d0b1f64b74b6c3a571d6203093d71e5f62` | User-supplied compilation of product hypotheses | Preserved as source material; no thesis is accepted as fact without verification |

The original compilation is stored at [`docs/research/reuse/СКРИНЕР_epub.md`](../research/reuse/СКРИНЕР_epub.md). It is intentionally preserved in Russian and was not converted into a product specification. The repository copy is an intake artifact, not proof that the described product exists.

`calendar-spread` declares MIT licensing. No license file was found in the inspected `moex-tool` or `futures-screener-ui-2` snapshots; their code is therefore not copied into this repository and is treated as reference material unless a separate reuse permission is established.

## Executive conclusion

The most valuable reuse is a set of small patterns, not a repository merge:

1. Use the `moex-tool` ISS adapter pattern for board discovery, ticker aliases, paginated history, explicit field selection and layered local caching.
2. Use `LEGALCLOSEPRICE` for equity/ETF daily analytical closes when the field exists, with a recorded fallback to `CLOSE`. Do not apply this rule to futures without verifying the futures-specific price role.
3. Keep futures settlement, last trade, official close, bid/ask and live mark as different observations. A single generic `close` field is insufficient for VM accounting and execution research.
4. Reuse `calendar-spread`'s futures catalog and calendar-spread mapping as a candidate ingestion fixture. Rebuild it from dated source snapshots; do not accept hardcoded expiries, current-margin backfills or hand-entered fees as historical truth.
5. Adopt a dividend event pipeline with official-first evidence, source availability status, manual reviewed overrides, conflict reporting and split normalization. A price gap can validate or challenge an event; it cannot establish the event date or amount by itself.
6. Treat the `futures-screener-ui-2` product flow and deterministic data-audit discipline as future presentation guidance. Its universe, margins, volumes, histories and strategy results are explicitly mocks.
7. Treat the compilation's decision-cockpit, POOL → SHORTLIST → VALIDATE → BOOK → MONITOR and skeptical-validation ideas as hypotheses already compatible with the architecture. The compilation's exact market facts and “take ISS as the golden standard” language remain unverified.

## 1. Price and market-data methods

### 1.1 `calendar-spread`: direct ISS futures collection

`scripts/fetch_iss.py` uses the official ISS host and retrieves:

- the FORTS securities snapshot, including `PREVSETTLEPRICE`, `MINSTEP`, `LASTTRADEDATE`, `PREVOPENPOSITION`, `LOTVOLUME`, `INITIALMARGIN`, limits, `STEPPRICE`, settlement fields and fee fields;
- daily futures candles through the FORTS candles endpoint;
- daily spot candles and RUONIA/RUSFAR inputs through separate endpoints.

Useful implementation decisions:

- raw JSON is retained before CSV materialization;
- futures and spot endpoints are kept separate;
- the source path is simple enough to reproduce offline from committed fixtures;
- the catalog contains both economic and operational fields needed by the specification registry.

Limitations that prevent direct adoption:

- the script uses a fixed date range and a hand-maintained list of series;
- it does not paginate history or discover boards dynamically;
- it stores no response hash, source publication/update timestamp, local receipt timestamp or request manifest;
- daily futures values are taken from the candle `close` without explicitly distinguishing last trade from settlement price;
- expired-contract specifications are supplemented by calculated fallback dates and current values;
- the repository's current margin is reused for historical analysis and then scaled by price in `frozen_capital.py`; this is a scenario approximation, not historical ГО evidence;
- the list contains current snapshots, not a point-in-time catalog of what was knowable before each decision.

**Decision:** reuse the endpoint family and field inventory, but implement them behind a versioned ISS adapter with pagination, raw-response hashing, immutable source vintages and explicit price roles.

### 1.2 `moex-tool`: generic ISS history adapter

`data/fetcher.py` provides the most reusable ingestion pattern:

- resolve ticker aliases before requests;
- discover available boards from `/securities/{secid}.json`;
- select a primary/relevant board with a documented priority rule;
- request history in pages using `start`/`limit`;
- request only named fields;
- distinguish historical official daily data from an unfinished current session;
- cache boards and candles in SQLite with TTLs and a data-version invalidation mechanism;
- use a live current-session candle only as a separately labelled observation.

For shares and ETFs it selects `LEGALCLOSEPRICE` when positive and falls back to `CLOSE`; for currencies, indices and rates it uses `CLOSE` because the legal-close field is absent. This is a good instrument-specific price policy, not a universal MOEX rule.

**Decision:** adopt the pattern for spot/ETF and reference-rate adapters. Extend the contract so each observation states `price_role` (`legal_close`, `last_trade_close`, `settlement`, `bid`, `ask`, `live_mark` or another declared role), source vintage and quality status. Do not silently rename every value to `close`.

### 1.3 Futures prices: separate analytical roles

The inspected projects expose an important boundary but do not fully solve it:

| Role | Intended use | Required treatment |
|---|---|---|
| Settlement / `PREVSETTLEPRICE` or equivalent | Variation-margin and economic carry reconstruction | Prefer the exchange/NCC settlement field with its effective timestamp; never replace it with a last trade without a labelled approximation |
| Daily last-trade close | Exploratory spread chart and sensitivity | Keep as a separate field; it does not prove executable entry or exit |
| Official/legal close | Equity/ETF analytical close when supplied | Use only where the source defines the field for that instrument |
| Bid/ask or spread quote | Delayed virtual execution | Require contemporaneous two-sided evidence; candles cannot substitute |
| Current-session live mark | Monitoring only | Store receipt time, source delay and session state; never treat as a historical official close |

The first implementation must therefore ingest a raw row with all available source fields and derive role-specific normalized observations. The role used by a strategy and the role used by accounting must be recorded in the run manifest.

### 1.4 Exchange-recognized calendar spreads

`calendar-spread` preserves MOEX calendar-spread pages and multileg dictionaries. Its idea of treating an exchange-recognized calendar spread as a distinct instrument is valuable: a native spread can have a different quote, fee and margin path from manual legging.

The stored page and CSV are dated observations, not a permanent interface contract. Before use, the adapter must confirm:

- the current spread instrument identifier and leg direction;
- whether the user's broker exposes it;
- quote and trade history availability;
- fee and margin treatment for the relevant date;
- whether virtual execution is modelled as one native spread or two independent legs.

If any item is unknown, retain both execution scenarios and report the unknown rather than selecting the cheaper one.

## 2. Dividend and corporate-action methods

### 2.1 What `moex-tool` does well

The dividend subsystem in `data/dividends.py` and `sync_dividends.py` includes several patterns worth reusing:

- ISS dividends are attempted first, but a missing `dividends` block is treated as **source unavailable**, not as “no dividends”;
- smart-lab and okama are used as fallbacks with source labels;
- a manually reviewed seed can override or supplement automated sources;
- records retain `source` and `fetched_at` in SQLite;
- duplicate events are merged with amount/date tolerances;
- preferred shares are not blindly redirected to ordinary shares;
- known split factors and price-series normalization are handled explicitly;
- conflicts and suspicious shifted duplicates are reported instead of silently overwriting the reviewed seed;
- tests cover source priority, unavailable sources, split normalization, preferred shares, shifted dates, cache behavior and parallel rebuild deduplication.

These are strong operational safeguards for a local research tool.

### 2.2 What cannot be accepted as point-in-time evidence

The implementation also shows boundaries that must be made explicit in the new project:

- `registry_date - 2 days` is a derived convention, not a general corporate-action rule and must not be stored as the factual ex-date;
- a price-gap-derived “resolved ex-date” is a validation result, not an issuer or exchange event timestamp;
- `fetched_at` records when the current program observed a source, not when the event was publicly available historically;
- a current fallback fetch can introduce later knowledge into a historical backtest unless the event's availability vintage is preserved;
- secondary sources and library data can support discovery or cross-checking, but cannot by themselves establish a trading decision's historical information set;
- a backward-adjusted price series applies later distributions to earlier values and is unsuitable for a point-in-time signal unless the adjustment is explicitly a post-event analytical view;
- the price gate may reject bad data and expose conflicts, but a missing visible gap does not disprove a dividend.

### 2.3 Reusable event contract

The project should store separate fields rather than one ambiguous dividend date:

```text
corporate_action_id
issuer / security_id / instrument_link
action_type
announced_at
published_at
available_at
record_date
ex_date
payment_date
amount / currency / tax_status
source_id / source_locator / source_hash
source_role: primary | secondary | reviewed_seed | derived_check
confidence and conflict_status
effective_vintage / supersedes
```

Unknown fields remain unknown. `available_at` is required for point-in-time use; if it cannot be established, the event may be shown as a later reference but cannot silently influence a historical decision. `ex_date_resolved_by_price` may be stored as a derived diagnostic field and must never overwrite `ex_date`.

### 2.4 Dividend adjustment modes

The `moex-tool` forward/backward modes are useful for charting and explanatory analysis:

- forward mode shows a total-return-like path by reinvesting a distribution after the event;
- backward mode creates a historical adjusted series for visual comparability;
- tax can be a separate scenario.

For this project they remain reporting views, not the default futures signal input. A strategy must declare whether it uses raw prices, an event-adjusted analytical series or an economic futures formula with the dividend cash flow separately. The LQDT counterfactual and futures accounting must not receive an implicit dividend adjustment twice.

## 3. Reuse from the product/UX projects

### 3.1 `futures-screener-ui-2`

Useful ideas:

- decision cockpit rather than an unexplained ranking table;
- fixed, predeclared pool and visible POOL → SHORTLIST → VALIDATE → BOOK → MONITOR flow;
- separate pair dossier, validation, book, monitor and experiment journal views;
- provenance line on every metric;
- explicit PASS/BORDERLINE/REJECT gates and reason codes;
- strategy registry with parameter groups and presets;
- deterministic mock generation and a consistency audit that checks experiment counts, configuration hashes and symmetric correlations;
- skeptical mode that emphasizes worst-regime/out-of-sample behavior.

Limitations:

- `src/data/universe.ts` contains synthetic contracts, expiries, volumes, history lengths and a formula-generated ГО;
- `src/data/types.ts` is a useful UI vocabulary but not a validated market schema;
- all displayed results are deterministic mocks, even where labels resemble real MOEX values;
- the repository has no evidence that the UI values came from exchange data or that the product is executable.

**Decision:** preserve the workflow and audit concepts as presentation requirements. Connect them only to validated backend contracts and run manifests. Do not copy the mock universe or numerical defaults into the research engine.

### 3.2 `СКРИНЕР_epub.md`

The compilation contributes a coherent product hypothesis:

- correlation is a concentration warning, not a trade signal;
- parameter plateaus matter more than isolated historical peaks;
- RoM and stressed capital are more informative than return on nominal;
- WFO, purging/embargo, DSR/PBO and an experiment-count registry protect against selection bias;
- cost layers and native-spread-versus-legging execution should be compared;
- ML/RL should be overlays or research-only experiments, not the first strategy core.

These ideas align with the existing requirements and architecture and can guide later validation.

The compilation is explicitly not a finished product. Its exact session times, instrument counts, API claims, margin discounts, “gold standard” source language, strategy examples and numerical illustrations are hypotheses or dated claims until checked against applicable primary documents. They do not authorize implementation or trading.

## 4. Reuse matrix

| Candidate | Reuse now | Required adaptation | Do not reuse as |
|---|---|---|---|
| ISS URL patterns and field selection | Yes, as adapter input | Hash raw responses, paginate, preserve three clocks and rights status | Permission or unlimited access |
| Dynamic board discovery and aliases | Yes | Make board choice part of the source vintage and fixture | Permanent hardcoded board truth |
| `LEGALCLOSEPRICE` preference | Yes for shares/ETF where defined | Record field presence and fallback reason | Universal futures settlement rule |
| Current-session candle | Later monitoring only | Separate live mark from official history and include receipt time | Historical executable fill |
| Futures catalog fields | Yes | Historical snapshots, effective dates, contract identity checks | Historical margin/fee evidence from current snapshot |
| MOEX calendar-spread mapping | Candidate input | Verify codes, leg sign, broker access, quote history and margin | Guaranteed atomic execution for VTB/BCS |
| Dividend source chain | Yes as research adapter | Primary-first event schema, availability vintages, conflict state, rights | A clean truth table from secondary sources |
| Split factors and normalization | Yes as a quality module | Require documented corporate-action evidence and retain raw series | Proof that a price gap is a split |
| Forward/backward adjustments | Reporting views | Explicit mode in run manifest and no lookahead | Default point-in-time signal prices |
| LQDT/RUONIA instrument chain | Conditional historical extension | Label synthetic/proxy periods and keep actual LQDT separate | Matched account benchmark without user data |
| Frozen-capital engine | Conceptually | Replace approximated historical ГО and add event-sourced portfolio rules | Validated broker margin or loss cap |
| UI workflow and provenance | Yes, later | Bind to real contracts and rejection events | Market data or strategy evidence |
| Mock universe and values | No | Replace with dated catalog fixtures | Initial research facts |

## 5. Changes to previous stages

### Stage 4 — requirements

Added requirements for:

- distinct price roles for settlement, analytical close, last trade, quotes and live marks;
- a corporate-action event schema with availability time and source role;
- derived price-gap checks that cannot overwrite primary event dates;
- explicit adjustment mode and no-lookahead behavior;
- historical catalog and specification snapshots rather than current values retrofitted into old dates.

### Stage 5 — architecture

Added a price/event provenance boundary under point-in-time intake. The specification registry now records the selected price role and adjustment mode used by each evaluation. Corporate actions are immutable source observations with derived checks, not hidden preprocessing inside a strategy.

### Stage 6 — stack and data sources

The conditional baseline now selects:

- dynamic ISS board discovery, aliases, pagination and field-limited requests as the starting implementation pattern;
- `LEGALCLOSEPRICE` only for instrument classes where MOEX supplies that field and defines its meaning;
- separate futures settlement and last-trade paths;
- a versioned calendar-spread catalog and native-spread execution scenario;
- an official-first dividend adapter with reviewed seed and secondary cross-checks;
- synthetic LQDT predecessor chains only as labelled historical research extensions, never as a replacement for the matched actual LQDT benchmark.

No data source became approved for unattended collection or live execution as a result of this review.

## 6. Next implementation gates

Before the offline vertical slice accepts real source data, it must:

1. Save a small raw ISS fixture with request parameters, receipt time, content hash and source-rights status.
2. Rebuild the same normalized history offline from that fixture.
3. Demonstrate that the same instrument can expose settlement, last-trade close and live mark without field collision.
4. Reject a historical decision that relies on a later dividend correction or an event without `available_at`.
5. Show the difference between a native calendar-spread quote and a two-leg execution scenario without assuming either is available to VTB.
6. Verify contract identity by stable identifiers, expiry and source vintage; do not infer an expired series from a reusable short code alone.
7. Keep current ГО/fees separate from historical unknowns and render the latter as blocked or sensitivity scenarios.
8. Use one manually reviewed dividend fixture and one source-conflict fixture in contract tests.
9. Keep the LQDT benchmark on actual user-supplied or redacted statement/price inputs; label any synthetic predecessor chain separately.
10. Connect the future UI only to validated schemas and reason-coded gates.

Stage 8 turns these gates into the prioritized delivery sequence in [the delivery and validation plan](08-planning.md).

## Evidence limits

The repositories were inspected at the revisions listed above. No broker credentials, private account data, live orders or production services were used. The presence of a public endpoint, a committed fixture, a passing repository test or a plausible historical chart does not establish data rights, client-specific tariffs, executable fills, historical availability or profitability.
