# Web research: LLM skills and setups for medium-term futures arbitrage

**Research date:** 2026-11-03
**Scope:** country- and exchange-agnostic; medium-term relative-value, spread and arbitrage decisions in futures.
**Status:** external landscape review and design input; no strategy approval, broker authorization, execution evidence or performance claim.

## Executive conclusion

There is no mature, generally reusable open-source “LLM futures arbitrage trader” that combines:

- point-in-time multi-contract data;
- contract rolls, multipliers, currencies and expiry events;
- two-leg executable pricing and legging risk;
- margin, collateral and broker constraints;
- realistic delayed/partial fills;
- medium-term holding economics; and
- independently reproduced net performance.

The useful building blocks exist, but in separate ecosystems:

1. **Agent Skills and finance plugins** provide reusable instructions, scripts, references, workflows and human-review gates. They are strongest for research, documentation, evidence handling and auditability.
2. **MCP** provides a standard connection layer between an LLM application and data/tools. It is not a trading strategy, risk engine or permission model.
3. **Financial agent projects** such as TradingAgents, FinAgent, FinRobot and QuantHarness provide role decomposition, memory, reflection, multimodal analysis and structured reports. Their published experiments mostly cover equities, crypto or single-asset directional decisions, not two-leg futures arbitrage.
4. **Quant research platforms** such as Qlib, RD-Agent(Q), FinRL-X, LEAN and vectorbt provide the deterministic data, factor, backtest, portfolio and execution foundations. They are more relevant to the numerical core than LLM-native trading projects.

For this project, the best-supported architecture is therefore:

> deterministic spread and risk engine → candidate/event pre-filter → LLM evidence and hypothesis layer → deterministic validation and sizing → human-readable decision packet → human approval or a separately gated execution adapter.

The LLM should not be the source of truth for prices, P&L, hedge ratios, margin, sizing, expiry handling or order state.

## Evidence grading

- **A — implementation/specification:** public source code and/or protocol documentation that can be inspected and run.
- **B — research prototype:** paper plus code or detailed method, but limited market scope and no independent production evidence.
- **C — vendor/project claim:** useful design signal, not evidence of transferable trading performance.

Reported returns in papers and READMEs remain claims about their own datasets and protocols. They are not evidence for a different market, futures contract, spread, cost model or execution path.

## 1. What “skills” now mean

### 1.1 Agent Skills standard

The current Agent Skills specification defines a skill as a directory with a required `SKILL.md` and optional `scripts/`, `references/` and `assets/`. The metadata is loaded first, instructions are loaded when the skill activates, and detailed resources are loaded on demand. The specification recommends keeping the main instruction file compact and moving detailed material into references.

**Direct value for this project:** a spread-analysis skill can package the decision procedure without embedding live data or secrets in prompts. Scripts can perform deterministic calculations; references can contain contract-field definitions, validation rules and report schemas.

**Important limitation:** a skill is an instruction and resource package. It does not itself provide market data, historical permission, broker access, safe execution or statistical validity.

Source: [Agent Skills specification](https://agentskills.io/specification) (accessed 2026-11-03).

### 1.2 Anthropic skills and financial-services plugins

Anthropic’s public `skills` repository demonstrates dynamically loaded skills with instructions, scripts and resources. The separate `financial-services` repository is materially closer to the desired operating model:

- named end-to-end agents;
- vertical skill bundles;
- slash commands;
- MCP data connectors;
- managed-agent wrappers;
- validation and synchronization scripts; and
- explicit human sign-off boundaries.

Its financial workflows cover equity research, investment banking, private equity and wealth-management operations. The repository explicitly says its agents draft work product and do not make investment recommendations, execute transactions, bind risk or approve onboarding.

The reusable architectural idea is strong: **one domain skill source, several agent wrappers, explicit connectors, and a human approval boundary**. The domain content is not a direct futures-arbitrage solution. The listed provider connectors are mostly institutional financial-data products and may require subscriptions or API keys.

Sources:

- [Anthropic skills](https://github.com/anthropics/skills)
- [Claude for Financial Services](https://github.com/anthropics/financial-services)
- [Financial-analysis plugin layout](https://github.com/anthropics/financial-services/tree/main/plugins/vertical-plugins/financial-analysis)
- [Anthropic financial-services announcement](https://www.anthropic.com/news/advancing-claude-for-financial-services)

Grade: **A for the skill/plugin pattern; C for direct trading applicability**.

## 2. Connection and tool layer

### 2.1 MCP

MCP is an open protocol for connecting AI applications to external data sources, tools and workflows. The official reference servers include filesystem, fetch, memory, sequential thinking and time. The official server repository warns that reference servers are educational examples rather than production-ready solutions.

For a futures-spread project, MCP is useful as a controlled interface for tools such as:

- `get_contract_catalog(as_of)`;
- `get_candles(contract_id, interval, end_time)`;
- `get_quotes(contract_id, as_of)`;
- `get_event_calendar(contract_id, as_of)`;
- `compute_spread_snapshot(candidate_id, as_of)`;
- `run_walk_forward(test_id)`;
- `check_risk_budget(proposal_id)`;
- `render_decision_packet(proposal_id)`; and
- later, a separately permissioned `submit_order_intent()`.

The LLM should receive narrow, typed outputs rather than unrestricted database access. Read-only tools and calculation tools should be separated from any action tool. Each result should include an observation time, source/version, freshness, units, contract identity and uncertainty flags.

MCP does not solve:

- whether the data may be stored or used;
- whether a quote is executable;
- whether two legs can be filled together;
- broker or exchange margin treatment;
- authentication and secret handling; or
- whether a tool call is authorized to create market risk.

Sources:

- [MCP introduction](https://modelcontextprotocol.io/introduction)
- [MCP reference servers](https://github.com/modelcontextprotocol/servers)

Grade: **A for the connection protocol; not evidence of trading safety**.

### 2.2 Commercial financial MCP connectors

The Anthropic financial-services plugin lists connectors for providers including FactSet, Morningstar, S&P Global, Moody’s, LSEG, MT Newswires, Aiera and others. This demonstrates that commercial financial data vendors are beginning to expose agent-compatible interfaces. It does not establish free access, futures coverage, point-in-time history, rights to store data or suitability for a personal project.

The notable gap is a broadly maintained, exchange-neutral, free MCP server that exposes a complete two-leg futures lifecycle: historical contract catalog, rolls, quotes, trades, margin, calendars, order state and execution simulation.

## 3. LLM financial-agent projects

### 3.1 TradingAgents — best reference for role decomposition

The open-source TradingAgents framework uses LangGraph and separates:

- fundamental, sentiment, news and technical analysts;
- bullish and bearish researchers;
- a trader;
- risk-management agents; and
- a portfolio/fund manager.

It combines structured reports with short debates instead of passing an unbounded conversation history. The current repository also demonstrates model routing, persistent decision memory, checkpointing and a historical backtest command.

**Reusable patterns:**

- specialist reports with fixed schemas;
- explicit adversarial review;
- a risk gate before a portfolio decision;
- cheap/fast models for extraction and stronger models for synthesis;
- persisted decision and outcome memory; and
- a backtest that scores decisions after a stated entry convention.

**Limits for this use case:** the published paper evaluates a short stock sample, not futures spreads. The repository is research-oriented, relies on external market-data providers, and states that results depend on model, data and sampling. Its roles should not be copied literally: a “technical analyst” for a single stock is not a spread-model or contract-lifecycle specialist.

Sources:

- [TradingAgents repository](https://github.com/TauricResearch/TradingAgents)
- [TradingAgents paper](https://arxiv.org/html/2412.20138)

Grade: **B for the workflow; C for transferable trading performance**.

### 3.2 FinAgent — best reference for memory and reflection

FinAgent is a multimodal research prototype with:

- market-intelligence processing for numerical, textual and visual data;
- a diversified retrieval memory;
- low-level reflection on price movement;
- high-level reflection on previous decisions; and
- tool-augmented decision making.

The paper explicitly separates retrieval-oriented query fields from the summary used for a decision. That is an important design detail: the text that explains the current market is not automatically the right query for historical analogues.

**Reusable patterns:**

- separate current-state summary from retrieval keys;
- distinguish market-state reflection from post-trade reflection;
- store decision context and realized outcomes;
- make tools and expert rules explicit; and
- use a structured parser between model output and environment actions.

**Limits:** experiments cover stocks and crypto and are described as single-asset trading tasks. The memory mechanism can preserve a bad rule or a contaminated historical analogue unless the system stores time-safe evidence and validates retrieved cases.

Source: [FinAgent paper](https://arxiv.org/html/2402.18485).

Grade: **B**.

### 3.3 QuantHarness — closest time-horizon and futures-adjacent example

QuantHarness is a public LangGraph project and paper that uses four roles:

- Indicator Agent;
- Pattern Agent;
- Trend Agent; and
- Risk/Decision Agent.

It works from OHLC-derived information, studies 1-hour and 4-hour bars, and reports experiments that include Nasdaq futures and commodities. It is therefore closer to a medium-term futures workflow than most LLM trading demos.

**Reusable patterns:**

- compress raw bars into a typed indicator state;
- use separate numerical, structural and trend interpretations;
- require cross-agent consistency before a decision; and
- emit a structured direction, setup, rationale and risk-reward output.

**Critical limits:** it is a single-asset directional system, not a two-leg spread engine. The paper states that performance degrades on roughly 1–15 minute bars and that the system is not truly real-time because tool and LLM latency can exceed the opportunity window. Its decision setup assumes long/short direction and does not model a long leg and short leg with synchronized partial fills. Fixed stop-loss and reward assumptions are not portable to medium-term spread risk.

Sources:

- [QuantHarness repository](https://github.com/Y-Research-SBU/QuantHarness)
- [QuantHarness paper](https://arxiv.org/html/2509.09995)

Grade: **B for a useful adjacent prototype; not a production or arbitrage solution**.

### 3.4 FinRobot — best reference for deterministic computation and provenance

FinRobot’s current repository describes a progression from AutoGen educational examples to a PydanticAI/FastAPI/React desktop system. Its central principle is:

> models reason, software computes, agents orchestrate, and systems verify.

It separates deterministic financial calculations from LLM narration, tracks provenance, includes audit operators, and packages many domain playbooks. Its current scope is equity research and valuation, but the separation is directly applicable to spreads.

**Reusable patterns:**

- typed agent outputs;
- deterministic operators for every number;
- provenance and audit coverage;
- pipeline agents separated from debate agents;
- output-contract gates; and
- skill bundles maintained separately from agent wrappers.

**Limits:** the project is not a futures market simulator and its “production” label is the project’s own positioning, not independent assurance for another deployment.

Source: [FinRobot repository](https://github.com/AI4Finance-Foundation/FinRobot).

Grade: **A for software-architecture patterns; C for futures trading evidence**.

### 3.5 FinGPT and FinNLP — useful as narrow NLP components

FinGPT supplies open financial language-model recipes, LoRA fine-tuning examples, financial sentiment models, datasets and a forecasting demo. FinNLP supplies data-source and processing pipelines for news, social posts and company announcements.

They are relevant when an event/text feature is already justified, for example:

- classifying a dated policy or supply event;
- extracting contract-relevant facts from an official announcement; or
- normalizing multilingual event descriptions before deterministic scoring.

They are not a reason to place a generative model in the price/sizing loop. Their published datasets and examples are mostly equity/news-oriented; data-source availability, historical completeness, licensing and point-in-time correctness need separate validation.

Sources:

- [FinGPT repository](https://github.com/AI4Finance-Foundation/FinGPT)
- [FinNLP repository](https://github.com/AI4Finance-Foundation/FinNLP)

Grade: **A/B for NLP tooling and experiments; low direct relevance to spread execution**.

## 4. Quant research and execution foundations

### 4.1 Qlib

Microsoft Qlib is a modular quant research platform covering data processing, model training, backtesting, alpha seeking, risk modeling, portfolio optimization and order execution. It supports offline/online data modes, point-in-time data concepts, rolling retraining and multiple model families. Qlib also points to RD-Agent for automated quant R&D.

**Why it matters:** it provides the deterministic research substrate that LLM agent projects usually lack.

**Adaptation required:** the default examples and datasets are predominantly equity/cross-sectional. A futures-spread adapter would need contract identity, roll policy, pair construction, hedge ratios, leg-specific costs, expiry/holiday handling and an execution model.

Source: [Microsoft Qlib](https://github.com/microsoft/qlib).

Grade: **A for quant-research infrastructure; conditional for futures spreads**.

### 4.2 RD-Agent(Q)

RD-Agent(Q) is the strongest public example found for an LLM loop that researches quantitative strategies rather than directly improvising trades. It decomposes work into:

1. specification;
2. synthesis of hypotheses;
3. implementation by a code-generation agent;
4. validation through a real-market backtest; and
5. analysis and adaptive scheduling.

Its Co-STEER component records task/code/feedback, and the scheduler chooses whether to refine factors or models. The paper reports strong results on stock datasets under its own protocol, including additional out-of-sample experiments.

**Reusable patterns:**

- a formal specification before code generation;
- a fixed executable interface;
- a closed hypothesis–implementation–validation loop;
- persistent success/failure knowledge;
- deduplication of candidate factors; and
- explicit comparison against baselines.

**Required redesign:** for spreads, the generated object should be a candidate economic relation or deterministic feature, not an unconstrained “strategy”. Every generated candidate must pass a pair-level validator, time-safe walk-forward evaluation and execution stress before it can enter the research library.

Sources:

- [RD-Agent repository](https://github.com/microsoft/RD-Agent)
- [R&D-Agent(Q) paper](https://arxiv.org/html/2505.15155)

Grade: **A/B for automated research workflow; no direct evidence for futures-arbitrage transfer**.

### 4.3 FinRL-X

FinRL-X presents a weight-centric interface in which selection, allocation, timing and risk overlays all emit a target portfolio-weight vector that then flows through backtesting and execution. The repository advertises modular data, strategy, backtest and execution layers with pre-trade risk controls.

The interface idea is valuable, but a two-leg futures spread needs a richer contract than a simple asset-weight vector. The minimum equivalent is a **target-leg state** containing contract, side, quantity, multiplier, currency, hedge ratio, margin reservation, validity and roll status.

Source: [FinRL-X / FinRL-Trading](https://github.com/AI4Finance-Foundation/FinRL-Trading).

Grade: **A/B for modular interface design; limited direct applicability**.

### 4.4 LEAN, backtrader and vectorbt

These are not LLM projects, but they are important baselines because an LLM setup still needs a numerical environment.

- **LEAN** is a large event-driven engine with Python/C# support, local backtests and live-trading interfaces. It is a candidate for a derivatives-aware execution lifecycle, subject to data, licensing, broker and integration checks. Source: [QuantConnect LEAN](https://github.com/QuantConnect/Lean).
- **backtrader** supports multiple data feeds, multiple timeframes, commissions, slippage, volume filling, futures-like cash adjustment and scheduling. It is older and GPL-3.0, so license and maintenance fit must be checked. Source: [backtrader](https://github.com/mementum/backtrader).
- **vectorbt** is fast for parameter grids, signal research, walk-forward work and interactive analysis. The open-source version is well suited to deterministic screening; advanced futures contract multipliers, leverage and limit-order features are associated with VectorBT PRO. Source: [vectorbt](https://github.com/polakowo/vectorbt).

None of these automatically provides synchronized two-leg execution or broker-specific margin.

## 5. Research findings specific to medium-term futures arbitrage

### 5.1 The LLM’s highest-value jobs

The strongest use cases are tasks where language, evidence and workflow matter:

1. **Economic-link analyst:** explain why two contracts may share a driver, list conditions that break the relationship, and distinguish observed evidence from a hypothesis.
2. **Contract/event analyst:** normalize expiry, roll, delivery, settlement, holiday, corporate-action and macro-event information.
3. **Evidence verifier:** check that every material claim has a dated source and that the source was available at the decision time.
4. **Research orchestrator:** turn a candidate into a reproducible test specification and call the correct deterministic tools.
5. **Adversarial reviewer:** look for look-ahead leakage, duplicated signals, hidden exposure, bad units, unsupported assumptions and missing failure paths.
6. **Decision narrator:** explain a machine-computed proposal, its invalidation conditions and the reason for rejection when no trade is preferable.
7. **Post-trade reviewer:** compare the expected path with the realized path and classify the error as signal, regime, cost, data, liquidity, legging or operational failure.

### 5.2 Jobs that should remain deterministic

- contract catalog and historical availability;
- price/quote selection and timestamp alignment;
- continuous-contract construction and roll logic;
- spread definition and hedge ratio;
- z-score, cointegration or other statistical tests;
- expected holding time and alternative-capital cost;
- fees, funding, slippage and market-impact scenarios;
- margin and collateral availability;
- integer contract sizing;
- portfolio and benchmark accounting;
- stress loss through delayed/partial exit;
- signal freshness and invalidation;
- order state, reconciliation and duplicate prevention; and
- all final limits and action gates.

The LLM may propose or explain a calculation, but the system should reject any value that did not come from a typed calculation tool.

### 5.3 Why a single-agent “trader” is a poor fit

A single model can generate a plausible narrative while silently mixing:

- different expiries;
- different multipliers or currencies;
- settlement and spot assumptions;
- historical and current contract definitions;
- indicative and executable prices; and
- economic hedge effectiveness with margin-offset eligibility.

The two-leg state is also too large and too operationally specific for free-form debate to be the source of truth. A small number of specialized agents over a shared, immutable, typed snapshot is safer than a long conversation.

## 6. Recommended custom skill set

These are design recommendations derived from the reviewed patterns, not existing off-the-shelf skills.

| Skill | Main output | Deterministic tools it must call |
|---|---|---|
| `market-data-provenance` | point-in-time data snapshot and evidence map | catalog, candle/quote fetch, timestamp and freshness checks |
| `futures-contract-lifecycle` | contract/expiry/roll status | contract master, calendar, roll and settlement rules |
| `spread-candidate-review` | economic relation and candidate status | pair universe, exposure map, correlation/cointegration diagnostics |
| `spread-statistics` | normalized spread state and regime diagnostics | hedge ratio, residual, stationarity, half-life, stability tests |
| `cost-and-capacity` | net opportunity range | fees, spread, impact, funding, holding-time and capacity calculators |
| `two-leg-execution-stress` | delayed/partial-fill loss envelope | quote depth if available, legging scenarios, exit path simulator |
| `risk-and-sizing-gate` | approve/reject and maximum integer size | margin, reserve, stress budget, portfolio and benchmark accounting |
| `signal-freshness` | valid-until and invalidation conditions | current snapshot, elapsed time, spread movement, event changes |
| `decision-packet` | compact human-facing proposal | schema validator, provenance renderer, report generator |
| `post-trade-review` | classified outcome and reusable lesson | execution ledger, expected-vs-realized attribution, memory store |

Each skill should have:

- a short `SKILL.md` with activation conditions and workflow;
- a `references/` directory for formulas, field definitions and policy text;
- `scripts/` or package functions for numeric work;
- a versioned JSON schema for inputs and outputs;
- explicit `unknown`, `not available` and `not executable` states;
- a falsification/rejection section; and
- no credentials or implicit network permissions.

## 7. Recommended agent topology

### 7.1 Offline research mode

```text
Candidate generator (deterministic + optional RD-Agent loop)
        ↓
Data/provenance agent ── Contract/event agent
        ↓
Spread-statistics agent ── Cost/capacity agent
        ↓
Execution-stress agent ── Risk/sizing agent
        ↓
Adversarial reviewer
        ↓
Deterministic walk-forward/backtest
        ↓
Research report and candidate registry
```

The LLM proposes hypotheses and diagnoses results. The backtest decides whether the candidate passed the predeclared test.

### 7.2 Monitoring mode

```text
Scheduler → numeric scanner → candidate gate
                         ↓
                  fresh typed snapshot
                         ↓
       LLM evidence/reasoning only when material
                         ↓
              deterministic risk gate
                         ↓
             Telegram decision packet
```

The scanner should not invoke a large model for every candle or every pair. Call it when a candidate crosses a numeric threshold, an event state changes, the regime changes, or a human asks for an explanation. This reduces cost, latency and nondeterminism.

### 7.3 Execution mode

Execution should be a separate service and gate. A model-generated `BUY/SELL` string must never be accepted as an order instruction. The minimum interface is a validated, signed or otherwise authorized `OrderIntent` with:

- candidate and snapshot IDs;
- exact contract IDs and sides;
- integer quantities;
- maximum price/slippage assumptions;
- expiry and validity time;
- allowed order types;
- two-leg sequencing policy;
- abort and flatten conditions;
- risk-check result; and
- explicit authorization context.

The first LLM-enabled stage should remain notification and decision support. Direct execution is a later, separately gated capability.

## 8. Recommended practical setup options

### Option A — local-first and low-cost: recommended starting point

- custom Agent Skills following the open specification;
- one local MCP server exposing only read-only project data and deterministic calculators;
- SQLite/Parquet for snapshots and an append-only decision ledger;
- vectorbt or a compact custom engine for fast screening;
- a dedicated two-leg simulator for final tests;
- an LLM only for evidence, hypothesis review and explanation;
- Telegram only after the numeric risk gate; and
- no broker tool in the first iteration.

This is the best fit for the current project because it preserves offline reproducibility and avoids making a general agent framework the critical path.

### Option B — quant-research-heavy

- Qlib as the research/data workflow where its data model fits;
- RD-Agent(Q) ideas adapted to spread candidates and pair-level validation;
- custom futures contract and execution adapters;
- FinRobot-style deterministic operators, provenance and output gates;
- a separate LLM orchestration layer for hypothesis generation and review.

This can automate research iteration, but it requires substantial adaptation before it can understand futures rolls, integer legs and two-leg execution.

### Option C — agent-demo adaptation

- TradingAgents-style analyst/reviewer roles;
- FinAgent-style memory and reflection;
- QuantHarness-style compressed technical state;
- MCP tools for project data;
- deterministic risk and backtest services underneath.

This is useful for prototyping the user interface and decision narrative. It is the least suitable as the primary quantitative engine because its published setups are mostly single-asset and directional.

## 9. What should be rejected or deferred

Reject or defer these patterns for the current project:

- an LLM directly reading arbitrary raw market data and calculating the trade;
- free-form multi-agent debate without typed state;
- training or fine-tuning a finance LLM before proving an incremental task;
- chart-image reasoning as the primary spread signal;
- using a stock-focused financial plugin as proof of futures support;
- treating model confidence or consensus as a probability calibration;
- using paper backtest returns from another market as a feasibility estimate;
- automatic memory of past trades without time-safe and regime-aware retrieval;
- an MCP server with write/order tools enabled by default;
- treating broker/exchange margin offset as implied by economic hedge quality; and
- adding news sentiment before a dated, deterministic price/event baseline has been beaten.

## 10. Research gaps to carry into implementation

1. Find or implement an exchange-neutral futures contract schema with historical versions.
2. Define the exact two-leg snapshot and execution-state contracts.
3. Implement a conservative delayed/partial-fill simulator before any LLM comparison.
4. Benchmark LLM-assisted hypothesis generation against manually specified spread features.
5. Measure whether text/event extraction adds value after price, roll, cost and regime baselines.
6. Test retrieval memory with strict decision-time cutoffs and a contamination audit.
7. Compare one structured agent against multiple agents; do not assume debate improves results.
8. Measure model latency, token cost, repeatability and failure rate on the actual workload.
9. Keep a registry of rejected candidates and reasons to prevent repeated rediscovery and selection bias.
10. Validate the whole-account/LQDT comparison and project risk policy independently of the LLM layer.

## 11. Source register

| Source | Type | Main use |
|---|---|---|
| [Agent Skills specification](https://agentskills.io/specification) | protocol/spec | skill packaging and progressive disclosure |
| [Anthropic skills](https://github.com/anthropics/skills) | reference implementation | skill structure and examples |
| [Claude for Financial Services](https://github.com/anthropics/financial-services) | financial plugin | agents, skills, commands, MCP connectors, human gates |
| [MCP introduction](https://modelcontextprotocol.io/introduction) | protocol docs | tool/data connection model |
| [MCP reference servers](https://github.com/modelcontextprotocol/servers) | reference code | server boundaries and warning about production readiness |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | code + paper | role specialization, debate, memory, checkpoints, backtest |
| [FinAgent](https://arxiv.org/html/2402.18485) | peer-reviewed conference paper | multimodal tools, memory, reflection |
| [QuantHarness](https://github.com/Y-Research-SBU/QuantHarness) | code + paper | price-driven agents, 1h/4h setup, futures-adjacent scope |
| [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | code | deterministic computation, provenance, skills and gates |
| [FinGPT](https://github.com/AI4Finance-Foundation/FinGPT) | code + models | finance NLP and lightweight adaptation |
| [FinNLP](https://github.com/AI4Finance-Foundation/FinNLP) | code | dated news/social/announcement pipelines |
| [Qlib](https://github.com/microsoft/qlib) | code + docs | quant research, risk, portfolio and execution workflow |
| [RD-Agent](https://github.com/microsoft/RD-Agent) | code + paper | automated hypothesis–implementation–validation loop |
| [R&D-Agent(Q) paper](https://arxiv.org/html/2505.15155) | research paper | factor/model co-optimization and validation design |
| [FinRL-X](https://github.com/AI4Finance-Foundation/FinRL-Trading) | code + paper | modular strategy/backtest/execution contract |
| [LEAN](https://github.com/QuantConnect/Lean) | code | event-driven backtest/live-engine reference |
| [backtrader](https://github.com/mementum/backtrader) | code | multi-feed, commission, slippage and futures-like simulation |
| [vectorbt](https://github.com/polakowo/vectorbt) | code | fast screening and parameter/walk-forward research |
| [The New Quant survey](https://arxiv.org/html/2510.05533) | survey | taxonomy, evaluation, leakage and governance guidance |
| [Automate Strategy Finding with LLM](https://aclanthology.org/2025.findings-emnlp.1005/) | peer-reviewed paper | risk-aware multi-agent factor generation and evaluation |

## 12. Method note

The initial burst of broad web-search requests was rate-limited with HTTP 429. The review was continued using direct fetches of primary GitHub, protocol, ACL and arXiv sources, with a narrower search cadence. This report therefore records a high-confidence source set and explicit gaps; it does not claim exhaustive discovery of every current repository or commercial product.
