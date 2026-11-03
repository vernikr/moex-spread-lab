# Stage 6 — Stack and data sources

Date: 2026-10-26

## Status and authority

Stage-6 selection is delivered as a conditional, evidence-based baseline. It is a starting configuration for the offline vertical slice, not a broker authorization, legal conclusion, profitability claim or live-trading approval.

The selection favors reproducibility, low operating cost, manual VTB feasibility and the ability to replace every provider through the Stage-5 contracts. The first implementation is deliberately narrow: one virtual portfolio, a small admitted universe and no broker write access.

## Selected operating model

| Area | Baseline selection | Status and limitation |
|---|---|---|
| Heavy research | MacBook workstation | Selected; broad history, trial runs and reports stay local |
| Lightweight runtime | Optional single-process worker on the existing VPS | Conditional on a measured workload and the server resource gates; no deployment in this stage |
| Initial execution mode | Manual VTB workflow represented by delayed virtual execution | Selected for the research model; no account connection or live order |
| Future automation | BCS Trade API as a later read-only-first candidate | Publicly documented interface, but account permission, data rights and collateral perimeter are unverified |
| Strategy scope | Economic spread families first; statistical mean reversion as a control | Selected research order, not strategy approval |
| Notifications | Telegram behind a durable outbox | Optional after offline replay is correct; delivery never authorizes exposure |
| User interface | Local/private reporting first; dark-only UI later if it earns its cost | No public dashboard or new VPS listener |

## Technology baseline

### Workstation

- Python 3.12+ as the common implementation language, provisioned reproducibly with `uv` rather than relying on the system Python version.
- Versioned source and configuration in Git; secrets and account material only through secure runtime configuration.
- Parquet or equivalent partitioned files for immutable research datasets, with raw source captures retained only where rights permit.
- DuckDB for local analytical queries and batch research; it is not part of the constrained VPS runtime.
- Deterministic command-line runs first. A UI is not a prerequisite for the first vertical slice.

### VPS, if later activated

- One bounded process for ingestion/monitoring, virtual-ledger projections and the notification outbox.
- Python standard-library-oriented runtime with SQLite for hot state and event projections.
- Rolling hot retention only; cold history and heavy analytical files remain on the workstation.
- systemd resource controls, no Docker, no public listener and no change to VPN/network services.
- Self-stop and alert on resource, disk, data-rights or notification safety failure.

This is an implementation baseline, not a claim that every package has already been installed or that the VPS has spare capacity for the worker. Stage-3 measurements are the current capacity evidence; a workload benchmark is still required.

## Data-source selection

### 1. MOEX ISS — conditional research source

Use as the first candidate for offline exploratory studies only, subject to the rights gate.

**Useful evidence from R04/R06/R11:**

- delayed current data is approximately 15 minutes, not execution-grade;
- daily futures summaries and contract metadata are useful for economic screening;
- minute candles can be useful as raw diagnostics, but do not establish historical bid/ask, depth or fills;
- historical coverage is uneven, live-series-dependent and vulnerable to reused short codes and changed session conventions;
- specifications and margin snapshots need dated preservation.

**Hard limits:**

- no historical bid/ask or order-book replay;
- no inferred fills from candle midpoint, close or settlement price;
- no claim that an answering endpoint grants storage, non-display, redistribution or profit-extraction rights;
- no automated signal-to-order path from ISS;
- no public display or redistribution of captured market data.

**Reuse implementation pattern from Stage 7:** discover boards before history requests, resolve documented aliases, paginate history, request an explicit field list and retain the raw response with request parameters, receipt time and hash. For shares/ETFs, prefer `LEGALCLOSEPRICE` when the source supplies and defines it; for futures, preserve settlement and last-trade fields separately rather than applying the equity rule.

**Project decision:** A-05 permits this private single-user local research pipeline to proceed when ISS data are publicly accessible. The assumption is recorded as project provenance, not as a legal conclusion. It does not authorize redistribution, a public service, broker use, unattended signal runtime or live trading.

### 2. MOEX and NCC primary documents — specification and event evidence

Use dated official documents for contract terms, calendars, settlement conventions, exchange fees, recognized spread groups and risk-parameter snapshots. The specification registry must preserve effective date, publication date, receipt date and source hash.

Current reports do not prove a complete historical risk-parameter archive or the broker's client-specific transformation. Missing dates remain unknown and are stress inputs, not zeros.

The futures catalog adapter may use fields such as `PREVSETTLEPRICE`, `LASTSETTLEPRICE`, `MINSTEP`, `STEPPRICE`, `LASTTRADEDATE`, `INITIALMARGIN` and `BUYSELLFEE`, but each field is a dated snapshot. Current ГО/fees cannot be retrofitted into expired contracts as historical facts. Historical approximations remain labelled sensitivity scenarios.

### 3. VTB/QUIK — manual execution evidence

VTB remains the initial manual broker because that is the user's stated operating route. The system will not assume a public VTB API. In a later shadow phase, VTB/QUIK observations may provide the executable leg prices, order status, fills, fees and manual timestamps needed to replace ISS proxies.

No login, key, account statement or order is needed for the offline slice. A future read-only evidence collection must first confirm the user's account configuration, applicable tariff, KPUR status, LQDT treatment, margin perimeter, notification path and access from the actual operating location.

### 4. BCS Trade API — future automation candidate

The reports document a read-only and a trading/read scope, HTTP/WebSocket market-data surfaces and explicit rate limits. This makes BCS a replaceable future adapter, not a current dependency.

The adapter remains disabled until account-level authorization, data-use rights, token handling, collateral/recourse terms, order semantics, partial-fill behavior, reconnect/reconciliation and an independent human emergency path are evidenced. No token or API call is required at Stage 6.

### 5. LQDT benchmark inputs

Use a secure, user-supplied account/statement import or manually prepared redacted fixture for actual benchmark accounting. Do not substitute RUSFAR, a generic cash rate or an invented fixed yield for the matched LQDT path.

The benchmark adapter must preserve whole-account genuine external flows and distinguish economic LQDT value from usable collateral. A predecessor chain such as RUONIA → RUSFAR → VTBM → LQDT may extend exploratory history by linking returns, but those synthetic periods must be labelled and cannot replace actual LQDT data in the pilot benchmark. Account identifiers and statements never enter Git, logs or external model prompts.

### 6. Corporate and calendar events

Do not build a broad news/dividend subsystem before a selected pair requires it. For an admitted family, use a dated primary event source and the R12 point-in-time rule. A reviewed seed and secondary sources may fill discovery gaps or provide a conflict check, but source role and availability remain visible. Store announcement/publication/availability, record, ex and payment dates separately. A price-gap check can challenge or support an event; it cannot invent its factual ex-date. If an event cannot be shown to have been available before the decision, the affected signal is blocked or labelled inconclusive.

## Initial universe and research order

The project will not begin with an unrestricted all-pairs search. The first research registry contains:

1. `H0` — no-trade matched LQDT control;
2. same-underlying currency calendar/carry families, starting with CNY/RUB as the first feasibility candidate;
3. perpetual-versus-dated funding/basis families where the funding series and contract specifications are observable;
4. one liquid index/calendar family as a robustness comparison, conditional on dated dividend and specification inputs;
5. a simple statistical mean-reversion control, labelled as a falsification benchmark rather than an economic claim.

Commodity calendars, cross-asset pairs, equity ordinary/preferred relationships, regimes, text and neural modules remain later experiments. They may be plugged in through the same contracts, but they do not enlarge the first implementation scope.

The first data resolution is:

- daily data for the economic feasibility and long-axis research;
- hourly or 10-minute derived observations for robustness and delayed-signal sensitivity where permitted data support them;
- M1 only as raw diagnostic input, never as proof of executable historical fills;
 - no historical order-book or fill collection: A-08 fixes the offline input to candles plus delayed proxy scenarios; actual execution is a separate future operational gate.

This resolution reflects the reports' common conclusion that free-data lag plus 3–90 minute manual response makes very short-lived opportunities unverifiable.

## Tariff and cost configuration

Tariffs are effective-dated provider artifacts, not constants inside strategies. The initial cost registry contains separate scenario profiles:

| Profile | Use | Treatment |
|---|---|---|
| `VTB_USER_TARIFF_CURRENT_TO_2026_12_31` | Manual research scenarios | User-confirmed `Мой Онлайн Прайм`: VTB broker commission for derivatives is zero through 2026-12-31; exchange fee remains instrument-dependent; margin/account controls remain unresolved |
| `VTB_PUBLIC_CURRENT` | Sensitivity only | Current public conditions with effective dates and unresolved account applicability |
| `BCS_TRADER_SCENARIO` | Future broker comparison | Documented public scenario; account level and fixed-fee applicability remain unknown |
| `COSTS_INCOMPLETE` | Safety default | Blocks any claim of net advantage or exposure-increasing action |

Every cost profile must cover both legs and both directions, exchange/clearing charges, broker charges, spread-order rules, execution friction, roll/expiry/forced-close costs, financing, LQDT transactions and unknown components. A missing value is a rejection or sensitivity branch, never zero.

The 90-day research window must be able to cross tariff effective dates. A run records the applicable schedule at each event rather than applying today's schedule retroactively.

## What is selected versus what remains conditional

| Decision | Selection | Remaining gate |
|---|---|---|
| Research location | Laptop-first | Measure run time/storage on the actual fixture |
| Runtime language | Python 3.12+ | Provision and test reproducible environment |
| Local analytics | Parquet + DuckDB candidate | Confirm rights, dependency footprint and fixture performance |
| VPS state | SQLite + bounded single process candidate | Workload benchmark and resource cap test |
| Manual broker | VTB | Account/tariff/collateral and execution evidence |
| Future API broker | BCS adapter candidate | Read-only access, rights, account and order semantics |
| First market data | MOEX ISS conditional research source with dynamic board/history adapter | Written rights/permission interpretation and reproducible access check |
| Price roles | Separate settlement, analytical close, last trade, quote and live-mark fields | Instrument-specific field mapping and historical availability |
| Corporate actions | Official-first event adapter with reviewed seed and secondary cross-checks | Availability vintages, source rights and conflict handling |
| Executable prices | Outside the offline project input under A-08 | The system reports candle-close proxies with reaction-delay and cost sensitivity; no historical fill claim |
| First pair family | CNY/RUB same-underlying carry/calendar candidate | Current contract, margin, liquidity, costs and integer feasibility |
| Notifications | Telegram outbox adapter | Offline replay first; user delivery and outage test later |

## Stage-6 acceptance gates

Before implementation proceeds beyond fixtures, the selected baseline must demonstrate:

1. A clean run can be rebuilt from a pinned module/artifact manifest with no network access.
2. The same source vintage produces the same normalized observations, strategy proposals and LQDT accounting.
3. A missing tariff, specification or executable-price requirement produces a named blocked/inconclusive result; A-05 supplies the local private-research assumption for the ISS rights flag.
4. The initial candidate universe can be narrowed or expanded through configuration without changing portfolio-core code.
5. A tariff provider can be replaced with a second scenario and the report shows the difference without changing strategy logic.
6. The data layer distinguishes event, publication and receipt time and rejects reused contract codes, stale data and post-decision corrections.
7. The virtual portfolio replays delayed manual execution using conservative bounds and never fabricates bid/ask, depth or fills.
8. The local run stays within workstation resource limits; no VPS deployment is needed to pass the offline gates.
9. A rights or broker-perimeter failure blocks public/unattended automation and live conclusions while leaving local analytical artifacts reproducible under A-05.

## Business assumptions carried forward

No new business decision is required to start the offline slice. The working defaults are:

- reliability and evidence take priority over maximum pair breadth;
- manual VTB operation is the first real-world workflow;
- BCS automation is a later option, not a current commitment;
- a zero-trade or inconclusive result is acceptable;
- no paid data purchase is assumed, but a blocked free-data right may require either a user decision to fund an authorized source or a narrower manual research mode;
- the initial virtual allocation remains RUB 30,000 all-in and does not include emergency funding for entries.

The main business question for a later decision is whether to prioritize a wider research universe or pay/authorize a higher-quality executable data source if the free-data rights gate remains unresolved. The architecture supports either choice without replacing strategy or portfolio modules.

## Evidence basis and limits

This selection uses the supplied R01, R02, R04, R05, R06, R07, R08, R10 and R11 reports, together with the previously accepted architecture review. They are provisional intake material. Public accessibility, documented formulas and reported observations do not prove the user's individual tariff, account perimeter, data rights, executable access or profitable strategy.

Stage 7 revised this selection in [the reuse analysis](07-reuse-analysis.md): dynamic ISS board/history handling, explicit price roles, an official-first corporate-action adapter and labelled synthetic LQDT predecessor chains are now part of the conditional baseline. Stage 8 will turn the selected baseline into a delivery and validation plan.
