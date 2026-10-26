**R15  |  OPTIONAL NEURAL STRATEGIES UNDER LIMITED COMPUTE** 

# **R15 — Optional neural strategies under limited compute** 

#### **Research report | MOEX futures-spread project | English** 

**Research/access date:** 22 October 2026 **Task-context pack date:** 7 October 2026 

**Status:** Evidence review and proposed protocol only. No model training, backtest, broker/account verification, production probing, or performance measurement was performed. 

## **Executive decision** 

**Conclusion: a neural model could add value in principle, but this project has no current evidence that it does. Neural** 

**modeling should be an optional, late-stage falsification experiment, not the default architecture.** On the supplied data class —free end-of-day MOEX futures data, tens to a few hundred contracts, incomplete/delisting-affected histories, and few independent economic episodes—the principal risk is statistical and execution validity, not the cost of running a small model. Inference for a small network is likely to be manageable on the Intel Mac CPU, but that says nothing about whether its predictions are useful or tradable. 

The public literature does not settle the question for this universe. Gu, Kelly and Xiu report useful machine-learning results on a much richer U.S.-equity panel—nearly 30,000 stocks over 60 years—not this MOEX futures setting [S6]. A 45-dataset benchmark found tree methods remained very strong on medium-sized tabular datasets and did not establish a universal winner [S7]. These findings justify comparing models; they do not establish transfer to MOEX spreads. 

**Recommended decision gate:** first establish that the candidate trades can be labelled at a genuinely executable time and that their full costs can be bounded. Then compare no-trade, a fixed deterministic spread rule, regularized linear models, and one tree model under the same chronological splits and portfolio simulator. Test **at most one small, pooled neural candidate** only if those baselines reveal a credible residual pattern, the data-sufficiency audit shows enough independent episodes to assess a meaningful uplift, and the final holdout can remain untouched. The neural model must beat the strongest non-neural model **and** the matched-LQDT portfolio counterfactual after fees, spread/slippage, missed/partial fills, lag, finite capital and the agreed risk rules. A higher prediction score alone is not a pass. 

The most important possible project change is not a larger network: it is to change the data/execution design if daily OHLCV cannot represent the delayed entry and fill process. MOEX documents public delayed ISS access and interfaces for candle/history data [S1–S3], but those documents do not prove historical intraday coverage for every intended contract. Until a read-only coverage audit proves otherwise, intraday availability, exact data latency and realistic execution-price reconstruction remain unknown. 

## **1. Scope, evidence status and limits** 

The only project input read was the self-contained prompt, including its embedded assumptions and risk/funding rules. There were no attachments, repository materials, earlier reports, private data, account credentials, or broker confirmations to inspect. Public source pages listed in the evidence register were reviewed on 22 October 2026. Where a source has an explicit publication/effective date, that date is retained below; a current page accessed in October does not prove that the same page contents were available on 7 October unless its own date/version supports that claim. 

No MOEX data endpoint was queried for a sample contract, no timeframe coverage was measured, no tariff schedule was calculated for the user’s account, and no training/inference/memory benchmark was run. All proposed experiments below remain proposals. The hardware and budget details are prompt-provided assumptions, not independently verified system measurements. The report does not establish trading feasibility or provide an investment recommendation. 

## **2. Define the decision and outcome before choosing a model** 

### **2.1 Prediction unit and decision time** 

Define an opportunity as a **pre-specified economically defensible spread** (or a controlled candidate set of such spreads), with its hedge ratio, eligible contract months, roll/expiry treatment, entry/exit logic and risk unit fixed before model comparison. Do not create a large family of candidate pairs after inspecting results. A spread is not necessarily market-neutral just because it contains two legs; basis, multiplier, maturity, liquidity, margin and leg-execution risks must be described. 

At decision time (t), features may use only information that was actually available then: prior completed OHLCV bars, open interest only where published and timestamped, lagged spread statistics, lagged volatility/volume/interest changes, known calendar/expiry variables, and contemporaneously available cross-sectional information. Store each field’s source timestamp and availability timestamp separately. A value that is eventually shown as belonging to date (t) is not automatically information available at the time the alert could have been acted on. 

A practical initial objective is to estimate the expected outcome of a **fixed-risk, one-lot or normalized spread opportunity over one pre-registered holding horizon** . Fit on a training-only normalized target if contract economics differ substantially, then convert predictions into expected RUB under the contract multipliers, tick values and integer-position constraints. A sign 

Research only • No backtest or hardware benchmark performed  |  Page 1 

**R15  |  OPTIONAL NEURAL STRATEGIES UNDER LIMITED COMPUTE** 

classifier is a useful diagnostic, but it is weak as a decision rule: a predicted positive sign can be smaller than round-trip costs or too uncertain to compensate for delayed execution and margin risk. 

### **2.2 Cost-aware label and action linkage** 

For a trade opportunity, record the realised or simulated one-unit outcome as gross spread P&L minus entry/exit fees, exchange and clearing charges, spread crossing/slippage, roll/expiry costs where applicable, and any explicitly modelled leg/partial-fill effect. Use the first executable price after the **decision’s true information-availability time plus the relevant action delay** . Do not silently fill at the signal close, bar midpoint, or a price observed before the action could occur. If a leg is rejected, stale or only partially filled, record that result rather than replacing it with an ideal fill. 

The model’s label is a training aid; the acceptance metric is portfolio-level. For every model and baseline, build the same integercontract virtual portfolio subject to RUB 30,000 all-in, initial/variation-margin liquidity, reserve requirements, finite positions and human intervention capacity. The virtual portfolio attempts every eligible signal and records rejected, stale, unfilled and partially filled attempts; it must not selectively omit awkward signals. 

The primary result is the daily/equity-curve **RUB excess over the matched LQDT counterfactual** , with identical true external flows. The counterfactual keeps funding diverted to the pilot invested in LQDT. Account for LQDT return on eligible collateral in the ledger only once; do not subtract it again as a second opportunity-cost penalty. Where collateral eligibility or the amount actually diverted is not verified, represent alternatives as scenarios rather than pretending the accounting is settled. Compare model candidates on the exact same accounting convention. 

### **2.3 The main objective and diagnostics** 

Primary metric: terminal cumulative excess RUB versus the matched LQDT counterfactual, with its full path and drawdown. Report, at minimum, cumulative net RUB excess, worst excess-curve drawdown, cumulative LQDT shortfall, turnover/fees, number and concentration of trades, capital/margin usage, unfilled/partial-fill rate, variation-margin stress and cost-scenario sensitivity. Report gross P&L and each cost component separately so an apparent improvement cannot hide missing costs. 

Secondary diagnostics include prediction loss, ranking quality, calibration and forecast-error distributions. Those diagnostics can explain a result; none is evidence of economic value without an identical execution/portfolio simulation. 

## **3. Required model ladder** 

**No-trade.** Purpose: test whether activity is better than preserving the LQDT counterfactual. Required comparison: it must remain a live comparator, not a placeholder. 

**Fixed deterministic spread baseline.** Purpose: test the pre-existing economically motivated logic with few fixed parameters. Required comparison: freeze the rule before model tuning and apply the same eligible universe, fills and costs. 

**Regularized linear model.** Purpose: test whether stable, approximately additive predictors explain the opportunity. Ridge/elastic-net regression or a simple calibrated classifier is enough. Required comparison: fit normalizers and regularization on training windows only. 

**One tree model.** Purpose: test nonlinear thresholds and interactions on tabular features using a restrained random forest or gradient-boosted tree implementation. Required comparison: use the same inner chronological validation budget as the other candidates. 

**At most one small neural model.** Purpose: test whether a compact network captures repeatable residual nonlinear interactions the prior models miss. Required comparison: proceed only after the earlier rungs and data sufficiency pass; beat the **best nonneural** model, not only the linear model. 

A relevant tabular benchmark is cautionary rather than dispositive: Grinsztajn, Oyallon and Varoquaux benchmarked 45 datasets and report that trees remained state of the art on medium-sized datasets (around 10,000 samples), while discussing why deep learning’s relative performance is not universal [S7]. Gu et al. found promising trees and neural networks in a large equity crosssection [S6]. Together these sources argue for a fair ladder rather than either “neural always wins” or “neural can never help.” Neither paper tests this MOEX strategy, small-capital execution, the user’s costs or the LQDT counterfactual. 

## **4. Data sufficiency and leakage controls** 

### **4.1 Count independent episodes, not rows** 

A panel with 200 contracts across 1,000 dates may contain many rows but far fewer independent economic observations. Contracts can share the same underlying risk driver, react to the same news, and be held concurrently; neighbouring labels overlap when positions persist for several days. The effective evidence is closer to the number and diversity of independent market episodes and executed portfolio decisions than the number of contract-date rows. 

Before model work, produce a data audit with: distinct dates; usable dates by contract and instrument family; contract listing/expiry/delisting coverage; missing/stale values; open-interest availability; trading-calendar/session conventions; source and publication timestamps; pairs with overlapping legs; count of candidate signals; overlap among holding windows; 

Research only • No backtest or hardware benchmark performed  |  Page 2 

**R15  |  OPTIONAL NEURAL STRATEGIES UNDER LIMITED COMPUTE** 

regime/event coverage; and the number of independent contiguous blocks that actually contribute to outcome variation. Do not declare sample sufficiency solely by crossing a row-count target. 

There is no defensible universal minimum independent-episode count that can be specified without the variance of net outcomes and the smallest economically meaningful uplift. Before training, define that minimum valuable uplift in RUB and run a block-based power/sensitivity analysis using only development data. If the observed history cannot plausibly distinguish that uplift from noise, classify the experiment as **underpowered** and stop or extend the history—do not widen the architecture search to manufacture a result. 

### **4.2 Panel, time and contract leakage** 

All observations for a date (across contracts and candidate pairs) belong to that date’s chronological fold. Never randomly split rows from the same date across train and test. If multiple candidate spreads share a leg or a market shock, score them inside a shared portfolio simulation and use time-block uncertainty estimates; do not treat them as independent wins. 

Purging/embargo must be based on the label’s actual information interval. If a label opened at (t) and exits at (t+H), training outcomes whose holding/label intervals overlap the test interval must be removed; leave a further embargo where justified by feature/position overlap. A generic time split gap helps, but a row-count gap alone does not correctly handle unequal holding times or panel labels. Scikit-learn describes TimeSeriesSplit as time-ordered and exposes a gap parameter; its default gap is zero [S9]. Implement and audit the stronger, strategy-specific purge explicitly. 

Fit imputation, scaling, winsorisation, feature selection, target normalisation, probability calibration and all learned transformations on the training portion of each fold. Use rolling features whose windows end strictly before the decision time. Do not use full-sample mean/variance, future cross-sectional membership, future contract metadata, final-history adjusted prices that encode later rolls, or labels built using prices unavailable after the signal. Persist the exact historical instrument universe and contract definitions used in every fold, including expired contracts where relevant. 

### **4.3 Regime shifts and selection risk** 

Use expanding or rolling chronological walk-forward tests and report by calendar block and meaningful market regime. The panel may contain repeated observations from one macro driver rather than many independent experiments. Use a date-block or event-block bootstrap for confidence intervals, resampling all linked instruments within a block together; do not bootstrap individual panel rows as if independent. 

Log every tested baseline variant, feature set, label horizon, execution assumption, neural architecture, seed, early-stopping decision and hyperparameter trial. Preserve a final later-period holdout whose results do not choose the model. Looking at that holdout and then changing the architecture or threshold consumes its status as untouched evidence. 

Bailey, Borwein, López de Prado and Zhu’s _The Probability of Backtest Overfitting_ explains how selecting among many backtests can create selection bias; its 2015 revised public manuscript defines PBO/CSCV around the relationship between in-sample and out-of-sample strategy rankings [S8]. This is useful for understanding trial-count risk, not a magic adjustment that can recover missing independent regimes. PBO/CSCV can be a secondary diagnostic if there are enough strategy configurations and comparable return series; it must not become the selection target. A deflated-Sharpe-style statistic is also secondary and is not a substitute for the locked-holdout RUB/LQDT test. 

## **5. Neural candidates and compute fit** 

### **5.1 Candidate scope** 

If the gates pass, try **one pooled, shallow MLP on engineered tabular features** , trained from scratch on the project’s own permitted market dataset. Share parameters across instrument opportunities; do not start with a separate model per contract. Keep it small and regularised, with a fixed and pre-registered capacity, early stopping based only on inner chronological validation, and a small bounded tuning budget. Treat width/depth/regularisation/seed combinations as trials and log all of them. If instrument-family metadata is used, it must be known at decision time and should not quietly encode future contract identity. 

Do not start with a Transformer, large pretrained model, LSTM/GRU ensemble or a large architecture sweep. The prompt’s data class does not demonstrate enough independent episodes to justify those degrees of freedom. A small causal temporal CNN could be a later, separate hypothesis only if the data audit shows long, contiguous histories and the tabular baselines fail to represent a specific lagged pattern. That would be a new preregistered test, not a free extra model inside this one. 

### **5.2 Hardware and inference** 

The Intel MacBook Pro has a six-core Intel i7, 16 GB RAM and a Radeon Pro 5300M with 4 GB VRAM according to the supplied context. A small MLP’s CPU training/inference is the simplest first experiment; this report has no measured runtime or memory result. The neural model should be evaluated on CPU first because deployment does not need GPU throughput for a daily small panel. 

Do not assume PyTorch’s mps backend accelerates this Intel Mac: Apple’s current PyTorch-on-Mac setup page lists Apple silicon for the stable MPS path [S10]. Apple’s TensorFlow Metal plugin documentation allows Mac computers with AMD GPUs but says 

Research only • No backtest or hardware benchmark performed  |  Page 3 

**R15  |  OPTIONAL NEURAL STRATEGIES UNDER LIMITED COMPUTE** 

Intel-GPU acceleration is unsupported and warns that small networks may run faster on CPU [S11]. Whether the exact Radeon Pro 5300M, OS, library versions and required ops work together has not been verified. No CUDA path is assumed. 

The VPS is listed as 1 vCPU, 1 GB RAM plus 2 GB swap, with other workload and a shared 250–350 MB background budget that is unverified. Do not choose a dependency-heavy neural runtime for it without a later, measured resource test. Future deployment candidates must demonstrate peak resident memory, cold-start/runtime, disk footprint and offline inference under the **actual** shared budget. A lightweight exported artifact or tree/linear model may be easier to operate, but exporting is not automatically correct: predictions and missing-value handling must be compared against the reference implementation within pre-declared tolerances. No deployment or production probing is proposed now. 

### **5.3 Optional free compute is not an entitlement** 

**Local Intel Mac CPU.** What is known: only the hardware stated in the prompt; no benchmark performed. Practical use: preferred for all baseline work and a small neural proof-of-concept. No compute charge is assumed. 

**Google Colab free tier.** Official documentation says free access can include GPUs/TPUs, but resources and usage limits fluctuate and are not guaranteed [S12]. Practical use: optional short experiment only. Do not plan reproducibility or production around GPU availability. 

**Hugging Face Spaces CPU Basic.** Current documentation describes CPU Basic as 2 vCPU/16 GB with no hourly hardware charge, while also stating that creating a compute-backed Gradio/Docker Space requires a paid plan; paid hardware requires billing or a grant [S13]. Practical use: not a dependable free training entitlement. Do not create a Space just to train this model. 

**Paid GPU/VPS upgrade.** Not required by the proposed small-model experiment; no account, card or grant is assumed. Out of scope unless future evidence shows the experiment cannot reasonably run on CPU and a justified budget is approved. 

Do not upload private broker, account, position, or proprietary strategy data to third-party notebooks/Spaces. If external compute is used, use only public or synthetic data, verify the provider’s current terms and data location, and keep secrets out of notebooks/logs. This model does not need a pretrained foundation model; downloading a neural model is not a substitute for having adequate labelled MOEX examples. 

## **6. Reproducibility, uncertainty, licensing and operations** 

A reproducible run requires a frozen raw-data snapshot and checksum; a source/timeframe/contract coverage manifest; contract specifications and version dates; explicit data licensing/redistribution review; exact feature and label definitions; split boundaries and purge/embargo manifest; all fee/slippage/delay scenarios; dependency lockfile; operating system and CPU/library versions; model architecture and hyperparameters; random seeds; training logs; and a trial registry. Include a short model card recording intended use, limitations, training dataset, training parameters and evaluation—these are the topics HF’s model-card documentation calls out [S14]. Record the applicable licenses for code, libraries, model artifacts and data separately; an open-source framework does not grant rights to redistribute exchange data or third-party model weights. 

Same-seed reproducibility must be tested in the same frozen environment. PyTorch documentation cautions that complete reproducibility is not guaranteed across releases or platforms, and even CPU/GPU results can differ [S15]. Use deterministic settings where available, persist the trained artifact and reference predictions, and define acceptable numeric tolerances instead of promising bit-identical results on every platform. 

For uncertainty, begin with out-of-fold forecast residuals and calibration diagnostics, with uncertainty intervals estimated over time blocks rather than row-wise independent errors. A small sample may make calibration unreliable; where so, the valid response is to broaden the no-trade region, reduce claims or abandon the model—not to report false precision. Consider a position only when its conservative expected benefit exceeds the relevant stress-cost hurdle and the action remains inside the fixed portfolio/risk rules. Reject or fall back when required features are stale/missing, inputs are out of the training range, output checks fail, or drift/calibration triggers fire. 

The fallback must be deterministic and fail closed: a failed model load, version/checksum mismatch, non-finite output, missing expected feature, or failed calibration/drift check means no neural-generated new exposure. Use the already-tested baseline only if it remains independently eligible under the same risk gates; otherwise do not trade. Monitor input-distribution changes, forecast calibration, outcome residuals, execution slippage and actual-vs-simulated fills. Monitoring coverage is not emergencyresponse coverage; human availability, trade quota, and escalation responsibilities remain undefined and must be specified before any future live gate. 

## **7. Staged experiment, cost and resource matrix** 

No mandatory monetary spend is needed for the proposed research sequence. The cost of data collection, manual contract audit and experiment execution is time; exact time requirements are not measured. Do not assume an unverified free intraday history or use missing costs as zero. 

### **Stage 0 — Data and execution feasibility** 

**Work/resources:** Read-only audit of MOEX ISS endpoints and each intended contract; check history depth, bar intervals, OHLCV/OI completeness, timestamp/availability, expired instruments and terms. Collect dated contract specifications, fee 

Research only • No backtest or hardware benchmark performed  |  Page 4 

**R15  |  OPTIONAL NEURAL STRATEGIES UNDER LIMITED COMPUTE** 

schedules, margin/risk parameters and broker schedules. Local CPU; no model. 

**Deliverable:** Coverage matrix and a decision-time-to-executable-price diagram, including every unknown. **Advance/discard: Stop** if data cannot support an honest label or if lawful/accessible history is inadequate. Do not infer historical intraday coverage from the generic ISS API page. 

### **Stage 1 — Label and simulator** 

**Work/resources:** Freeze one economically defensible candidate universe, horizon, entry/exit, spread construction, integer sizing, margin/liquidity reserves and partial-fill/stale/rejected rules. Implement conservative/base/stress fees and slippage as parameters. 

**Deliverable:** Reviewed label definition and identical portfolio ledger for all candidates. **Advance/discard: Stop** if results depend on close/midprice fills, unaffordable lots, impossible margin, discretionary signal omission, unmodelled legging, or unspecified external cash-flow accounting. 

### **Stage 2 — Non-neural ladder** 

**Work/resources:** Run no-trade, deterministic, regularized linear and one restrained tree model on the same expanding/rolling walk-forward splits. CPU/local environment. Freeze the trial log. 

**Deliverable:** OOS fold ledger, diagnostics, block-based uncertainty and baseline ranking in net RUB. **Advance/discard:** If no simple model beats the counterfactual after costs, stop. If the best simple model is not reproducible or evidence is underpowered, do not add neural complexity. 

### **Stage 3 — One neural challenge** 

**Work/resources:** Only after Stage 2 passes, train one small pooled MLP from scratch on CPU. Same labels, features, folds, execution and capital constraints. Bounded hyperparameter budget and fixed seeds; no architecture zoo. **Deliverable:** Reference artifact, complete trial log, incremental OOS comparisons and resource measurements. **Advance/discard: Discard** if no material incremental uplift over the best non-neural model, if uplift vanishes in stress costs/lags, if it depends on a few episodes, or if reproducibility/fallback fails. 

### **Stage 4 — Locked final holdout** 

**Work/resources:** Freeze model, features, thresholds, cost assumptions, code, data snapshot and reporting rules. Evaluate all remaining candidates once on a genuinely later untouched holdout. No tuning after reveal. **Deliverable:** Final pre-registered table of net LQDT excess, delta versus best baseline, risk, execution and uncertainty. **Advance/discard:** Pass only if the pre-defined economic floor and confidence/risk rules are met. If the holdout is used to change the model, classify it as consumed and require genuinely new data for confirmation. 

### **Stage 5 — Future paper/shadow gate (deferred)** 

**Work/resources:** Only after new explicit project gates: read-only monitoring or paper execution, with timestamps and real rejected/unfilled/partial events. Re-verify broker/API permissions, human response and risk ownership. **Deliverable:** Live-time validation record, not a claim of actual performance. 

**Advance/discard:** Not authorized or performed by this report. Live automation and production deployment stay deferred. 

### **Proposed ablations (pre-register only those needed)** 

**Features:** core engineered spread/volatility/liquidity features; then remove open interest; then remove cross-sectional features. Every variant is a trial. Use only features with documented as-of availability. 

**Capacity:** one fixed small MLP versus the best tree and linear model. No repeated widening after a failed result. A later temporal CNN is a new experiment only if a specific sequence hypothesis survives. 

**Execution:** conservative/base/stress costs; action-delay scenarios spanning the supplied 3–90 minutes; feed/data latency kept separate from manual response latency; partial/rejected/unfilled fills; next-session gaps where relevant. If only daily data exist, label the delay simulation as a proxy and do not claim it reproduces a 3–90-minute path. 

**Portfolio:** identical RUB 30,000 all-in starting constraint, integer lots, all eligible signals attempted, finite margin and variationmargin reserve. Report a no-emergency-funding entry test. Every model uses the same gross-emergency allowance and restrictions if the simulator reaches an explicitly approved risk-reduction event; emergency funds never support a new entry, averaging down or resetting a loss limit. 

**Stability:** results by chronological fold, regime and instrument family; remove one influential event/block; evaluate sensitivity to a small number of legitimate fee/slippage choices fixed before the final test. 

### **Cost/risk rules to preserve unchanged** 

Use the embedded pilot governance as fixed simulation constraints, not parameters for the model to optimize: 

Starting allocation: RUB 30,000 all-in, including margin, variation-margin liquidity and reserve. Candidate positions must fit using integer contracts; the much larger same-account LQDT holding does not create permission to treat core collateral as test funding. 

Research only • No backtest or hardware benchmark performed  |  Page 5 

**R15  |  OPTIONAL NEURAL STRATEGIES UNDER LIMITED COMPUTE** 

Warning at RUB 5,000 excess-curve drawdown; action at RUB 10,000 drawdown from the excess-result high-water mark or cumulative LQDT shortfall of RUB 10,000; outer shortfall boundary RUB 15,000; formal review after 90 calendar days without loss reset. 

Emergency additional allocation is at most RUB 15,000 gross cumulative, for at most RUB 45,000 total gross contributed funding. Every use requires explicit approval and may only avert disorderly liquidation / complete de-risking of existing positions. It must never fund fresh exposure, averaging down or loss-resetting. Entry must pass with zero emergency funding. At the RUB 10,000 cumulative-shortfall trigger, any approved funding is only to reach flat. Unapproved broker use of core collateral is an incident; core collateral is not silently treated as pilot money. 

Preserve the identical-external-flow LQDT counterfactual, keep diverted pilot funding invested in the counterfactual, and do not double-deduct LQDT return on eligible retained collateral. Only the loss of the long-term holding deduction is excluded by the prompt; tax residence and account treatment are unknown. 

The MOEX fee page lists exchange/clearing transaction fees and notes an asymmetric maker/taker arrangement [S5]. Actual broker commissions, client category, margin eligibility and contract-level fees are not verified. Parameterize the missing amounts and stop if conservative values erase the result; do not assign zero because a fee was not found. 

## **8. Falsification criteria and possible project changes** 

The neural hypothesis is falsified for this project (or remains unproven and must be discarded) if any of the following occurs: There are too few independent episodes to estimate the pre-defined economically meaningful uplift, even if row counts look large. 

Daily OHLCV cannot reconstruct a credible entry/exit and the project cannot obtain lawful, sufficiently complete intraday/executable-price history. In that case, either redesign the rule as a next-session strategy that daily data can honestly simulate, or stop the test; do not fake intraday fills. 

The no-trade, deterministic, linear or tree alternative matches or beats neural portfolio excess after the same costs and constraints. 

Neural uplift is not positive on the locked holdout, the block-based lower confidence bound for incremental uplift does not clear the pre-registered floor, it reverses under plausible stress costs/delay, or nearly all its gains are from one episode. As a pragmatic robustness flag (not a universal statistical law), treat any result where one contiguous event/block supplies more than half of the total uplift as concentrated and not yet reproducible. 

The result depends on future-fitted preprocessing, universe/survivorship leakage, a favourable fill convention, unverified fees/margin, discretionary omissions or emergency funding at entry. 

Predictions are unstable or poorly calibrated, drift makes uncertainty unusable, or a failed model cannot reliably fail closed to the baseline/no-trade action. 

The artifact, data lineage, offline CPU inference, software/license constraints, or measured resource budget cannot be maintained without disproportionate complexity. 

Reasonable project changes if a gate fails: reduce the universe to fewer defensible and sufficiently liquid spreads; narrow the target to a strategy with clear economics and observable fills; obtain longer/different legitimate data; change the decision schedule so it matches the actual data availability (for example, next-session rather than immediate manual execution); or keep the deterministic/linear/tree system and omit neural methods. More architectures, GPUs, and parameter sweeps are not valid substitutes for independent episodes or real execution evidence. 

## **9. Reading of** **_Advances in Financial Machine Learning_** 

Marcos López de Prado, _Advances in Financial Machine Learning_ , first edition (Wiley, 2018; ISBN 978-1-119-48208-6) was located via the publisher’s table of contents, but the book’s full chapters were not read for this report. Accordingly, no book section is relied on as evidence and no claim is attributed to a section as if it had been read. The validation recommendations above instead cite the openly accessible Bailey et al. PBO manuscript [S8] and the scikit-learn time-split documentation [S9]. The book remains a useful optional lead, not a substitute for the project-specific validation and execution audit. 

## **10. Evidence register (source, date/version, supporting excerpt)** 

Access dates for undated live documentation are **22 October 2026** . Short excerpts are included to make the basis of material facts traceable; paraphrase and interpretation are separated from the source wording. 

**[S1] Moscow Exchange, “Program interface for ISS.”** Official MOEX documentation; undated live page, accessed 22 Oct 2026. URL: https://www.moex.com/a8531 Section: overview, lines 36–40. 

Excerpt: “market data … data for chart (‘candles’) creation … historical data”; access can be “free to access … with a latency.” Use/limit: establishes that ISS exposes relevant interface families and delayed public access exists; does not establish any specific contract’s data depth, finest usable timeframe, history continuity, or redistribution rights. 

Research only • No backtest or hardware benchmark performed  |  Page 6 

**R15  |  OPTIONAL NEURAL STRATEGIES UNDER LIMITED COMPUTE** 

**[S2] Moscow Exchange ISS, “ISS Queries,” reference version v.0.14.1, and candle endpoint reference 341.** Official interface documentation; undated live page, accessed 22 Oct 2026. 

URLs: https://iss.moex.com/iss/reference/?lang=en ; https://iss.moex.com/iss/reference/341?lang=en ; interval/date availability: https://iss.moex.com/iss/reference/333?lang=en 

Sections: /iss/engines/[engine]/markets/[market]/securities/[security]/candles; /candleborders. 

Excerpt: candle output is “HLOCV for candlestick charts”; a separate reference describes available intervals and dates. Use/limit: supports a proposed contract-by-contract coverage audit, not a claim that one-minute or any other intraday history exists for all target contracts. No endpoint was probed for actual coverage in this research. 

#### **[S3] Moscow Exchange, “О публикации расчетных цен по фьючерсам и опционам” [Publication of settlement prices for futures and options], 27 Mar 2026, 19:34 MSK.** 

URL: https://www.moex.com/n98831 Sections: daily settlement publication and SETTLEPRICE_CLR, lines 26–45. 

Excerpt: settlement prices are published daily; free data is available “с задержкой 15 минут” (“with a 15-minute delay”). Use/limit: documents a specific settlement-price feed’s delay regime and archive paths; do not generalize that statement to every market-data field, historical candle or third-party feed. 

**[S4] Moscow Exchange, “Московская биржа расширяет время торгов на срочном рынке” [MOEX extends derivatives trading hours], 10 Jul 2026; effective 14 Jul 2026.** 

URL: https://www.moex.com/n101980?nt=106 Section: session schedule, lines 26–38. 

Excerpt: from 14 July 2026, weekday derivatives trading is stated as “с 06:50 до 23:50” (06:50–23:50 Moscow time). Use/limit: schedule change matters for timestamps and manual delays; individual contract eligibility, holidays and any weekend session must still be checked from the current calendar and contract rules. 

**[S5] Moscow Exchange, “Тарифы срочного рынка ПАО Московская Биржа” [MOEX derivatives tariffs].** Official live tariff page; accessed 22 Oct 2026. 

URL: https://www.moex.com/a90 Sections: “Общая информация,” lines 79–87, and linked fee schedules. Excerpt: fees include “биржевая и клиринговая” transaction commission, execution/clearing fees and transaction charges; maker/taker commissions are asymmetric. 

Use/limit: proves costs have multiple components and depend on the fee schedule. User-specific broker fees, tariff category and contract amounts were not calculated or verified. 

**[S6] Shihao Gu, Bryan Kelly and Dacheng Xiu, “Empirical Asset Pricing via Machine Learning,”** **_The Review of Financial Studies_ 33(5), May 2020, pp. 2223–2273; published online 26 Feb 2020.** DOI: https://doi.org/10.1093/rfs/hhaa009 ; NBER working-paper record: https://www.nber.org/papers/w25398 

Sections: abstract; “An Empirical Study of U.S. Equities.” Excerpt: “nearly 30,000 individual stocks over 60 years from 1957 to 2016.” Use/limit: supports that neural and tree methods can add predictive value in a rich, long U.S.-equity research design; the market, dataset, horizon, costs and capital constraints differ materially from this MOEX futures pilot. 

**[S7] Leo Grinsztajn, Edouard Oyallon and Gaël Varoquaux, “Why do tree-based models still outperform deep learning on typical tabular data?”** **_Advances in Neural Information Processing Systems 35_ , 2022, Datasets and Benchmarks track.** URL: https://papers.neurips.cc/paper_files/paper/2022/hash/0378c7692da36807bdec87ab043cdadc-AbstractDatasets_and_Benchmarks.html 

Section: abstract; linked paper/supplement. 

Excerpt: benchmark uses “45 datasets”; results say trees remained state of the art on medium-sized data (“~10K samples”). Use/limit: reinforces strong tree baselines and non-universality of neural superiority on tabular data; not a finance-specific or MOEX result. 

#### **[S8] David H. Bailey, Jonathan M. Borwein, Marcos López de Prado and Qiji Jim Zhu, “The Probability of Backtest** 

**Overfitting.”** Public manuscript revised Feb 2015 (34-page PDF); published version cited as _The Journal of Computational Finance_ (2017), DOI 10.21314/JCF.2016.322. 

Public PDF: https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf ; publication record: 

https://scholarworks.wmich.edu/math_pubs/42/ 

Locations: public-manuscript abstract p. 2; multiple-testing discussion pp. 3–4; framework §§2.1–2.2, pp. 8–10; limitations discussion §5. 

Excerpt: “false positives” rise as the “number of tests conducted on the same data” rises. 

Use/limit: primary methodological evidence for logging trials and guarding against strategy-selection overfit; does not give an empirical estimate of this project’s PBO or remove the need for a genuine forward holdout. The PDF’s cover/abstract and framework pages were inspected; no numerical results from its examples are applied to MOEX. 

**[S9] scikit-learn, “TimeSeriesSplit,” stable API documentation v1.9.1 (page displayed at access), accessed 22 Oct 2026.** URL: https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html 

Sections: class description and gap parameter. 

Research only • No backtest or hardware benchmark performed  |  Page 7 

**R15  |  OPTIONAL NEURAL STRATEGIES UNDER LIMITED COMPUTE** 

Excerpt: the splitter avoids training on future data to evaluate past data; gap is the number of samples excluded from the end of the training set before a test set. 

Use/limit: supports chronological validation, but its ordinary gap does not automatically purge heterogeneous overlapping tradelabel intervals or panel-linked rows. 

**[S10] Apple Developer, “Accelerated PyTorch training on Mac — Metal.”** Live setup document, accessed 22 Oct 2026; current stable-release requirements shown as PyTorch 2.11.0. 

URL: https://developer.apple.com/metal/pytorch/ Sections: Metal acceleration and requirements. 

Excerpt: listed requirements include a “Mac computer with Apple silicon.” 

Use/limit: do not assume the PyTorch MPS route applies to the supplied Intel Mac. This is not a performance benchmark of the user’s hardware. 

**[S11] Apple Developer, “TensorFlow Plugin — Metal.”** Live setup/troubleshooting documentation, accessed 22 Oct 2026. URL: https://developer.apple.com/metal/tensorflow-plugin/ 

Sections: requirements and “Currently not supported.” Excerpt: requirements allow “Apple silicon or AMD GPUs”; the unsupported list includes “Acceleration for Intel GPUs,” and the page notes CPU can be faster for small networks. 

Use/limit: suggests an AMD/Metal experiment might be possible but does not verify the Radeon Pro 5300M + current OS/library combination or all required operations. 

#### **[S12] Google, “Colaboratory FAQ,” live FAQ, accessed 22 Oct 2026.** 

URL: https://research.google.com/colaboratory/faq.html 

Sections: free service and resource limits. 

Excerpt: free resources are “not guaranteed and not unlimited,” and “usage limits sometimes fluctuate.” 

Use/limit: optional experimentation only; not a compute SLA, production resource or proof that a GPU session will be allocated. 

#### **[S13] Hugging Face, “Spaces Overview,” live Hub documentation, accessed 22 Oct 2026.** 

URL: https://huggingface.co/docs/hub/spaces-overview 

Sections: hardware resources and CPU Basic. 

Excerpt: CPU Basic is listed as “2 vCPU” and “16 GB”; the page also states compute-backed Gradio/Docker Spaces require a paid plan to create. 

Use/limit: the current entitlement distinction means not to infer training access from the absence of an hourly hardware charge; account/subscription eligibility was not checked. 

#### **[S14] Hugging Face, “Model Cards,” live Hub documentation, accessed 22 Oct 2026.** 

URL: https://huggingface.co/docs/hub/model-cards 

Sections: What are Model Cards; model-card metadata; specifying a license. 

Excerpt: cards should describe intended uses/limitations, training parameters, datasets and evaluation results; license is a metadata field. 

Use/limit: a documentation checklist for an artifact record, not a license for any MOEX data or any downloaded model. 

#### **[S15] PyTorch, “Reproducibility,” live documentation, accessed 22 Oct 2026.** 

URL: https://github.com/pytorch/pytorch/blob/main/docs/source/notes/randomness.md Section: reproducibility and controlling randomness. Excerpt: “Completely reproducible results are not guaranteed across PyTorch releases … or different platforms.” Use/limit: supports frozen environments, seeds, artifact storage and tolerance-based cross-environment tests; deterministic flags alone are not proof of bitwise identity. 

## **11. What is established versus still open** 

**Established by this report.** Public official docs describe delayed MOEX ISS access, candle/history interfaces, dated derivativessession changes and multiple fee components. Published research establishes that machine learning can work in rich assetpricing settings, that tabular tree baselines are strong, and that extensive strategy search creates selection risk. Small CPU inference is a plausible experiment but has not been benchmarked. 

**Embedded assumptions, not verified measurements.** Free daily futures panel of tens to a few hundred contracts; incomplete/delisting-affected history; 3–90-minute reaction delay; RUB 30,000 pilot and the specified LQDT counterfactual/risk rules; laptop and VPS specifications/background load. 

**Open checks before a neural experiment.** Exact data coverage/history depth and legal terms by contract; finest defensible free timeframe; as-of publication timestamps; realistic execution-price/fill modelling; current contract/broker fee and margin values; eligible collateral accounting; independent-episode power; human intervention quota and response coverage; actual CPU/RAM/runtime measurements; tax/account details beyond the prompt’s specified exclusion. 

**Not done.** No real-data download or API probe, no data-quality audit, no target labels, no training, no backtest, no PBO/DSR estimate, no measured added value, no hardware benchmark, no broker/account/legal verification, no deployment or live trading. 

Research only • No backtest or hardware benchmark performed  |  Page 8 

**R15  |  OPTIONAL NEURAL STRATEGIES UNDER LIMITED COMPUTE** 

**Final answer to R15:** yes, in principle a compact neural model can be tested on this data class and hardware; no, current public evidence is insufficient to conclude that it adds economically useful, reproducible out-of-sample value. The rational next test is data/execution feasibility followed by identical non-neural baselines. A neural candidate survives only if it wins that controlled comparison on the locked portfolio-level LQDT excess metric after costs, lag, capital, uncertainty and selection risk. If those gates cannot be passed, omit it. 

Research only • No backtest or hardware benchmark performed  |  Page 9 

