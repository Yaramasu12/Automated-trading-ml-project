# Implementation prompt: trustworthy execution and evidence-based profitability

You are the lead engineer and quantitative researcher for this existing automated Indian-market trading application. Implement the requirements below in the repository. Deliver working, integrated, tested changes and reproducible research results. Work in dependency order and preserve progress across sessions. Do not stop after writing a plan, listing recommendations, or adding disconnected components.

## Objective and operating boundaries

Improve the platform's ability to discover and retain positive net expectancy after all costs, while preventing unintended exposure and accurately reporting uncertainty. Software correctness, validated trading edge, and operational readiness are separate outcomes. Do not claim a profit improvement unless a reproducible comparison demonstrates it. An experiment that finds no profitable strategy is a valid result.

Repository:
`C:\Users\Saikumar Yaramasu\repositories\Automated-trading-ml-project`

Start by reading `AGENTS.md`, `README.md`, and `audit_results/2026-09-13/AUDIT_REPORT.md`, plus the audit's JSON evidence and reproduction scripts. Reverify findings against the current code; the audit is dated evidence, not an instruction to assume every issue remains present. Inspect other applicable repository instructions before editing.

Keep production credentials, real account records, existing model artifacts, and running services intact. Implement and test in isolated PAPER/BACKTEST environments. Prepare deployment configuration and migration instructions as concrete reviewable artifacts. This prompt authorizes implementation and isolated tests; it does not authorize live orders, live arming, production database migrations, production credential changes, or replacing a running deployment. Do not reset existing balances, silently delete records, or mix test fills with forward paper history.

Preserve all required LIVE conditions: execution mode, enabled flag, configured broker credentials, authenticated arming, confirmation phrase, healthy risk checks, and required market-data readiness. Resolve any conflict between reduction-only emergency execution and existing invariants explicitly; do not silently weaken a live-order gate. No LLM may waive a deterministic safety gate.

Use the existing architecture and components wherever they work. Extend or repair them before adding replacements. Avoid an unnecessary microservice rewrite, duplicate strategy engines, speculative dependencies, or advanced-model scaffolding without a real data/evaluation path. Respect the existing service-refresh rules for replaced instrument masters and dynamically changing execution mode.

## Baseline evidence to reproduce

The September 2026 audit observed an active PostgreSQL-backed PAPER deployment with 312 fills, starting capital INR 1,000,000, equity approximately INR 965,949.14, and no open positions. The local SQLite file was a different, contaminated historical dataset. Do not treat either observation as current without checking.

Reproduce and classify these findings as confirmed, already fixed, or not reproducible:

- Caller-supplied `opens_position=False` allowed a new position to fill while the PAPER kill switch was active.
- Deployed authentication was disabled; the API was published on all host interfaces.
- Trade metrics dropped derivative lot multipliers: INR 500 ledger profit became INR 10 in a reproduction.
- Marketable simulated limit fills could violate their limit price.
- Transaction taxes were outdated; short-volatility charges used net structure credit instead of gross per-leg turnover.
- Generic backtests used current closing prices in opening risk state, mixed option premiums with underlying prices, and mishandled product holding periods and stops.
- Session loss baselines used cost-basis equity and reset on restart.
- The LIVE readiness check could prevent emergency reductions during a kill-switch state.
- The holiday calendar misclassified 14 September 2026 and other dates.
- The frontend strategy filter used the wrong request field and displayed a two-point synthetic equity curve as historical simulation.
- Council decisions and outcomes did not join; promotion evidence reported zero paper days despite existing fills.

The audit's fixed-default NIFTY short-volatility model changed from profit to loss when spread assumptions increased. This supports investigating costs; it does not prove a new filter or model will generate profit.

## 1. Establish isolation, baselines, and change tracking

Create an implementation status document containing requirements, evidence, owning modules, dependencies, test commands, and completion criteria. Record Git revision, environment versions, source/data hashes, configuration, and reproducible seeds for baseline experiments.

Identify the active database/backend and deployment version without exposing secrets. Keep read-only production observations separate from test/replay output. Ensure both pytest and the repository's unittest CI path cannot access production SQLite defaults, PostgreSQL databases, live brokers, or local model services unintentionally. The pytest autouse fixture alone does not protect unittest execution or imports before fixtures run.

Reproduce test failures accurately. Fix tests that rely on ambient feature flags by constructing explicit configurations. Do not skip or weaken assertions to turn CI green. Use meaningful tests for financial invariants and failure behavior, not tests that merely restate the implementation.

## 2. Repair authentication and control-plane security

Make safe authentication defaults explicit. Required authentication with an empty credential must fail closed. Keep any development bypass deliberate and visible. Apply equivalent authorization to REST and WebSocket commands and sensitive snapshots. Prevent secret exposure in responses, logs, frontend bundles, URLs, diagnostics, and exported reports.

Prepare secure deployment configuration with restricted bindings and documented credential provisioning/rotation. Do not replace production credentials during development. Review all mutation paths, administrative overrides, model promotions, manual approvals, mode changes, and kill-switch controls. A manual approval cannot bypass execution safety.

## 3. Derive risk reduction from real positions

Do not trust external metadata such as `opens_position`, `hedged`, `multi_leg_group`, priority, or `manual_approved` as proof of safety or approval.

Compute opening/reducing quantities from signed broker-reconciled holdings, pending fills, reserved reductions, and instrument identity. Prevent duplicate exits and concurrent requests from overclosing or reversing a position. Reject the opening remainder of a reduction-only request unless separately authorized as an entry.

Recheck under the appropriate lock/reservation immediately before broker submission, including after rate-limit waits. Validate finite positive prices, valid quantities, tick sizes, lot sizes, product types and contract status. NaN, infinity, malformed values, stale metadata, or user-supplied risk totals must not evade limits.

Test flat-book false closes, oversized closes, partial fills, simultaneous exits, duplicate requests, mode changes, and kill-switch activation between enqueue and submission. The original false-close reproduction must result in zero new exposure.

Design an explicit authenticated emergency reduction path that verifies quantity and retains necessary broker/configuration protections. Test its interaction with stale quotes, kill switch, daily losses and account insolvency. Do not make `force=true` a universal bypass.

## 4. Make accounting and financial metrics reconcile

Define unambiguous quantity units: lots, exchange units and historical contract multiplier. Persist the values needed to reconstruct each fill even after expiry or instrument-master refresh. Use explicit monetary precision and exchange rounding rules.

Define and separately report gross realized P&L, fees/taxes, net realized P&L, unrealized P&L, cash, collateral, margin, equity, deposits and withdrawals. Reconcile account equity to the fill ledger and explain adjustments. Handle long/short positions, partial closes, reversals, options premiums, futures variation margin, expiries and applicable settlement mechanics correctly.

Do not model futures as fully paid cash equities. Do not invent settlement prices or fabricate historical multipliers for incomplete records. Mark unresolvable history as incomplete.

Compute profit factor and win rate from appropriate round trips or complete option structures, not unrelated leg fills. Return explicit unavailable/undefined/infinite states where mathematically appropriate; do not replace infinite profit factor with the number of winning trades. Use a correctly sampled return series for annualization and drawdown.

Persist an IST-session baseline from reconciled marked equity, adjusted for external cash flows. Preserve it across intraday restarts. Realizing a prior unrealized loss must not double-count that loss as a fresh daily decline. Provide a migration dry run, backups, reconciliation checks and rollback instructions for SQLite and PostgreSQL.

## 5. Unify Indian-market charges and contract rules

Implement a shared, versioned, date-effective per-leg charge model used consistently by replay, PAPER, research and reporting. Verify current and historical rates using authoritative exchange and broker sources. Include applicable brokerage, STT/CTT, exchange fees, SEBI fees, GST, stamp duty, exercise/settlement charges and other relevant product-specific costs. Mark unavailable charges explicitly.

Model gross turnover for each buy/sell leg; net condor credit is not gross option turnover. Support exchange and product differences and historical changes. Store the schedule version with the result and calibrate against available contract notes without exposing account data.

Starting references, which must be rechecked at implementation time:
- https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax
- https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf
- https://www.nseindia.com/resources/exchange-communication-holidays

Use dated exchange calendars and contract specifications for NSE, BSE and enabled commodity segments. Cover holidays, special sessions, expiry shifts, lot-size changes, tick sizes, quantity limits, product eligibility, settlement/delivery constraints and applicable broker restrictions. Do not infer API compliance from a placeholder Algo-ID or a guessed broker request field; verify documented requirements and report anything unverified.

## 6. Strengthen data quality and historical provenance

Record exchange timestamp, receive timestamp, decision-availability timestamp, source, instrument identifier and quality flags. Detect stale/missing/out-of-order quotes, sequence gaps, duplicates, invalid OHLC, impossible spreads, crossed books and clock skew. Assess freshness for each traded option leg, not only the underlying or feed heartbeat.

Separate synthetic, cached historical, live and model-derived observations throughout storage and UI. Validate corporate actions, adjusted versus raw prices, symbol changes, expired contracts and point-in-time universe membership. Prevent survivorship bias and prevent future corporate-action or news information from leaking into past decisions.

Collect legally accessible historical option-chain snapshots with bid, ask, available size, volume/open interest when available, expiry and timestamp. Respect broker limits and cache data. If depth, tick history or an executable historical chain is unavailable, disclose the limitation; do not substitute generated values and label the run real.

## 7. Repair replay and backtesting

Create a deterministic event ordering: information becomes available, strategy decides, risk reserves exposure, order arrives, then execution occurs. Use an injectable clock while preserving IST production behavior.

Keep future closes/highs/lows out of opening decisions and risk state. Model opening gaps, entry-day stop/target behavior and ambiguous intrabar ordering conservatively. Enforce intraday square-off and delivery/overnight product rules.

Use option premium series or explicitly labelled pricing models for options; never fill an option at underlying spot or compare premium stops against underlying highs/lows. Respect actual contract lifetimes, settlement and futures rolls. Resample equity once per stated interval for performance metrics.

Share strategy/risk logic with production where appropriate and explicitly document remaining differences. Reproduce original defects with regression fixtures and show before/after results. Validate timestamps and execution assumptions, not just whether the ending P&L is positive.

## 8. Improve execution realism and order lifecycle reliability

Make simulated limits obey price constraints. Model uncertain fills, partial fills, fill delays, adverse selection and bounded order chasing using observable data. Never assume a passive order fills because a candle touched the price. If queue position is unknown, expose alternative fill assumptions.

Implement reliable idempotency across retries and restarts, partial-fill accounting, cancel/replace races, broker timeouts and late callbacks. Reconcile an ambiguous submission before retrying; a timeout is not proof an order failed. Handle rate limits and rejected orders without duplicate exposure.

For multi-leg options, track the structure and every leg, reserve worst-case intermediate exposure, verify protection actually filled, and define bounded repair/unwind behavior for failed legs. A group identifier alone cannot prove a hedge exists.

Prevent queued PAPER intents from becoming LIVE orders after a mode switch. Check for multiple deployed execution workers and implement the minimum necessary ownership/reservation mechanism to prevent duplicate trading. Avoid a distributed-system rewrite when a simpler reliable constraint suffices.

## 9. Attribute every rupee of performance

Create a performance attribution report per complete strategy/structure, underlying, expiry, entry session, holding period and data-quality bucket. Separate gross signal return, commissions/taxes, spread/impact, delayed exits, hedge costs and execution errors.

Show trade count and uncertainty for every bucket. Avoid presenting tiny slices as proven patterns. Record rejected opportunities and reasons so filters can be studied without silently hiding bad outcomes. Measure foregone opportunities in replay without claiming they were attainable fills.

Use this attribution to choose the next research experiment. Do not optimize the annual target or an attractive dashboard metric.

## 10. Research entry quality and trade frequency

Investigate whether estimated gross opportunity exceeds the full round-trip cost plus a justified uncertainty allowance. Estimate thresholds from prior training data and freeze them for evaluation.

Test bounded hypotheses about liquidity, spread relative to premium, time of day, days to expiry, event proximity and volatility conditions. Compare each filter against the same unfiltered baseline. Analyze reduced trading frequency and missed opportunities as well as apparent win-rate improvements.

Use a calibrated return/payoff model if available. Do not multiply uncalibrated strategy confidence by a target to invent expected value. When expected return cannot be estimated credibly, keep the feature in research rather than presenting it as a profit gate.

## 11. Improve volatility and option modelling

For short-volatility research, compare implied volatility with forecasts of realized volatility over the matching horizon. Use underlying-specific observations where possible; India VIX is not a universal implied-volatility measure for every index, expiry or commodity.

Evaluate smile/skew and term structure, bid/ask uncertainty, actual option expiry, and calibrated volatility forecast errors. Apply pricing sanity checks and reject surfaces inconsistent with available quotes. Use existing volatility/IV components when suitable.

Compare simple volatility baselines with existing HAR/GARCH or other supported candidates out of sample. A better volatility prediction is not automatically a profitable option strategy: assess the executable structure after all costs. Record fixed-lot and controlled-risk comparisons so changed sizing cannot disguise weak forecasting.

## 12. Research exits and tail-risk behaviour

Analyze complete-structure stop/target logic, maximum adverse/favorable excursion, holding time, expiry exposure, assignment/delivery constraints and costs of adjustments. Test a small prespecified set of exit variants on training data and evaluate frozen variants on unseen periods.

Compare structure-level and leg-level management while accounting for temporary unhedged exposure. Test price gaps, volatility jumps, spread widening and failure to close one leg. Do not assume the theoretical payoff cap guarantees that intraday unwind costs or temporary margin remain within the same bound.

Keep exits operational during agent/model outages. Record exit reasons and missed/late exits in performance attribution. Never widen a stop or increase exposure merely to recover a loss or meet the annual target.

## 13. Control portfolio concentration and capital efficiency

Calculate actual and reserved exposure from the portfolio, not caller-provided totals. Aggregate delta, gamma, vega, margin, scenario loss, expiry concentration and underlying/sector overlap using available data.

Treat correlated index short-volatility positions as shared risk. Evaluate combined spot/volatility shocks, correlation increases, liquidity loss and margin expansion. Keep a liquidity buffer and account for protection-leg failures.

Research position sizing separately from signal quality. Use conservative constrained sizing until payoff estimates are stable. Do not treat theoretical probability of profit or a small winning sample as sufficient input for aggressive Kelly sizing. Preserve daily-loss and drawdown limits.

Report return on total capital and capital actually committed, with correctly matched risk and exposure. Include appropriate cash/no-trade and simple market benchmarks. Do not portray leverage or a change in denominator as added predictive edge.

## 14. Evaluate regime and event filters as hypotheses

Test whether prior-observable volatility, trend, liquidity and event conditions explain strategy behavior. Candidate events include scheduled policy announcements, company results and contract-specific events when verified data are available.

Use the event's publication/availability time, not hindsight labels. Examine false positives and the return lost by avoiding trades. Keep model-regime filters advisory until incremental value is demonstrated; deterministic operational safety checks remain separate.

Do not create a large grid of filters and publish only the best combination. Track the total search and uncertainty.

## 15. Maintain a small, falsifiable strategy research backlog

After fixing data and costs, rank additional experiments by data availability, economic rationale, implementation effort and failure risk. Possible research families include volatility premium, trend continuation, mean reversion, cross-sectional relative strength or market-neutral relative value. These are hypotheses, not recommended trades or guaranteed improvements.

Select at most three initial experiments. For each, write the causal/economic rationale, required data, tradability constraints, baseline, search budget, rejection criteria and untouched evaluation period before testing. Include borrowing/short-sale constraints, financing, turnover and instrument eligibility where relevant.

Test diversification by joint portfolio P&L and stress exposure. Adding more symbols or strategies does not demonstrate diversification. Persist negative experiments to prevent repeating the same failed searches.

## 16. Make ML and LLM contributions earn their place

Keep the current no-edge return model unavailable unless it passes defensible validation. Do not train a regime classifier on its own rules and call that independent evidence. Keep saved/reloaded model acceptance, feature versions, training cutoff and dataset lineage verifiable.

Use chronological/purged validation where overlapping labels require it. Fit preprocessing only on training data. Evaluate predictive calibration and actual net trading performance against simple baselines. Test each feature/model by ablation under the same trades/cost assumptions where possible.

Join scan decisions, candidate intents, orders, fills and outcomes with stable identifiers. Validate the council's incremental contribution and latency/cost. LLM output must remain advisory and must not alter safety rules, self-promote models, or autonomously raise risk limits. Prefer deterministic operation if inference adds no demonstrated value.

## 17. Build defensible validation and promotion

Separate hypothesis development, parameter selection, and final untouched evaluation. Use walk-forward evaluation and appropriate purge/embargo for overlapping information. Track every tested variant, including rejected and losing ones. Include realistic benchmarks, zero-trade windows, and all costs.

Report net expectancy, profit factor, drawdown, tail losses, recovery time, turnover and statistical uncertainty. Use resampling that respects serial dependence and complete option structures; do not treat correlated legs as independent trades. Apply multiple-testing controls appropriate to the search. Existing DSR/PBO/Monte Carlo gates must be audited, not accepted merely because they return PASS.

Stress actual measured costs and adverse scenarios, including illustrative higher-cost cases. Specify acceptance thresholds and compute budgets before the final test; justify rather than invent universal profitable thresholds. Never keep retuning against the final holdout. Compare results at matched capital/risk.

Bind promotion to source revision, data version, strategy parameters, underlying family, expiry/holding rules and execution assumptions. Missing or incompatible evidence blocks new risk for that candidate while verified exits remain available. Derive qualifying paper sessions from immutable run records, excluding tests and replays.

Keep research/shadow/paper/live statuses truthful. A model-price backtest or elapsed 30 calendar days does not independently qualify a strategy for real-money use.

## 18. Build reliable forward-paper operations and observability

Create a clean separately identified forward-paper experiment without deleting prior records. Use a frozen configuration, real timestamped market observations, daily reconciliation and complete quote-to-outcome lineage. Observe at least the configured qualifying-session minimum and continue until the independent sample and regimes support meaningful evaluation.

Track feed health, stale symbols, broker response state, unresolved orders, blocked exits, unexpected exposure, session baseline integrity, drawdown and cost drift. Separate technical health from economic performance and data readiness. Run fault-injection tests for restarts, feed gaps, duplicate callbacks, missing options legs, unavailable inference and database failures.

Use `note_swallowed` where the repository requires observable continuation. Exclude expected cancellation/control flow deliberately. Alert on material faults with evidence; avoid fabricated LLM incident summaries or repetitive nonactionable alerts.

Maintain bounded retention and query costs; profile actual latency bottlenecks before optimizing. Keep long inference/research tasks off the execution loop. Protect account-specific data consistently across REST, WebSocket, logs and dashboards.

## 19. Repair the dashboard and user-facing truthfulness

Fix request/response contract drift, including strategy filters. Display actual equity curves, trade/structure logs, gross/net P&L, costs, uncertainty, data provenance and experiment/deployment version. Show why a trade was blocked or a model is unavailable.

Clearly distinguish targets, simulations, model-derived option prices, forward-paper results and live results. Replace the hardcoded annual-profit target with a user-configurable planning input that cannot drive automatic risk escalation. Never display a target as expected income.

Add meaningful frontend/API tests for filtering, authentication, mode changes, stale-data indicators, partial orders and performance units. A healthy HTTP response or a successful build does not prove that financial displays are correct.

## 20. Verification, deliverables and completion criteria

Implement in small, reviewable increments. Complete mandatory correctness/security/data work before interpreting strategy improvements. Keep speculative strategies disabled until earned. For every phase, report what changed, why, evidence, remaining limitations, and exact commands.

Required deliverables:
1. Integrated source changes and focused regression/failure tests.
2. Reproducible isolated backend pytest and unittest runs, plus frontend tests and production build. Explain remaining failures/skips without hiding them.
3. Migration dry runs, reconciliation reports, backups/rollback procedure, and safe deployment configuration staged for review.
4. Before/after accounting and replay comparisons with source/data/config hashes.
5. A strategy experiment register including failed experiments, unseen-period results, cost stress and uncertainty.
6. A forward-paper runner/runbook with immutable experiment IDs and daily reports; do not claim future sessions have already completed.
7. An updated API/frontend contract and honest capability/readiness report.
8. A final profitability assessment stating either supported improvement, no improvement, or insufficient evidence, with matched-baseline comparisons.

Minimum acceptance checks:
- A false close cannot open exposure, including during kill switch and concurrent execution.
- A valid reduction cannot reverse the position or be duplicated by retry/race conditions.
- Required-auth routes and sensitive WebSocket data fail closed.
- All filled quantities, historical lot multipliers, costs, ledger balances and metrics reconcile within justified rounding tolerances.
- Session loss calculations survive restarts and realization of existing unrealized P&L.
- Limit fills obey limits; unknown broker submissions are reconciled before retry.
- No future information or underlying/premium price substitution enters backtests.
- Exchange calendars/contracts/fees have sources and effective dates.
- Backtest UI reflects the requested strategy and actual provenance/equity path.
- Promotion requires matching evidence and qualifying paper observations.
- No claim of profitability relies on synthetic prices, uncalibrated confidence, selected winning variants, or an annual-profit target.

Continue all implementation and verification that can be completed within this scope. If a dependency such as real historical quotes, account-specific broker charges, deployment authorization or future paper sessions is missing, implement the supporting code and record that exact dependency. Keep dependent features disabled or labelled incomplete, and continue independent work. Never fabricate data or mark an unfinished phase complete.

Begin now by inspecting the current repository and reproducing the highest-severity findings in an isolated environment. Then implement the repairs in dependency order. Produce working evidence, not a promise of more profit.
## 21. Daily session-end model and agent learning

The following is an integrated requirement of this implementation. A standalone copy is available in `DAILY_LEARNING_PROMPT.md`.

Implement this specification together with `audit_results/2026-09-13/IMPLEMENTATION_PROMPT.md`. Preserve its safety, isolation, validation and deployment boundaries. This is a requirement to build and test the learning system; it does not authorize changing the running deployment or placing real-money orders.

Objective: after each completed trading session, analyze reliable observations, update eligible datasets, train/evaluate candidate models and agent configurations when justified, and prepare the best validated configuration for future PAPER sessions. Improve measured net expectancy and resilience where evidence supports it. Do not promise that retraining improves tomorrow's profit. Daily review must run; daily replacement of the active model must not be mandatory.

### A. Audit and integrate the existing learning paths

Inspect these starting points and follow their actual dependencies:
- `scripts/daily_scheduler.py`: already lists post-session review and daily/intraday retraining jobs.
- `scripts/train_return_forecaster.py`, `scripts/train_intraday_forecaster.py`.
- `scripts/scheduled_research_session.py` and the research harness.
- `trading_platform/neural/return_forecaster.py`, `intraday_forecaster.py`, `vol_forecaster.py`, `har_rv.py`, and serving code.
- `trading_platform/ai/meta_labeler.py` and the distinct `ai/ml_features/meta_labeling.py` implementation; establish which is trained ML and which is strategy scoring.
- `trading_platform/orchestrator/reflection_engine.py`, ProfitGuard, specialist crew and council supervisor.
- Trace, outcome-label, learning-journal, model registry and promotion services.

Document what each job currently does, what data it consumes, whether it changes weights/artifacts, and whether its output reaches serving. Repair missing integration instead of creating a duplicate scheduler or an unused trainer. Distinguish a model trained on market observations from a rule-based agent whose scoring weights or memory are updated.

### B. Implement a durable session-end workflow

Use an exchange-aware state machine, with durable run records:

SESSION_ACTIVE -> SESSION_CLOSED -> RECONCILED -> DATASET_READY -> EVALUATED -> TRAINED_OR_SKIPPED -> CANDIDATE_VALIDATED -> SHADOW_OR_PAPER_ELIGIBLE -> NEXT_SESSION_READY.

Also support explicit states for missing data, unresolved reconciliation, insufficient labels, validation rejection, resource timeout and operational failure. A completed review does not imply successful training or promotion.

Key each run by exchange/segment, trading-session date, dataset version and job version. Use a database lease or equivalent ownership mechanism, idempotent stages, bounded retries and checkpoints so duplicate scheduler processes/restarts cannot double-count labels, rewards or promotions. Support retrying one failed stage without rerunning completed mutations.

Trigger from the verified exchange calendar and observed session completion plus an ingest/reconciliation grace period. Keep the existing clock injectable in tests and use IST consistently. Support holidays and special sessions. Do not treat the NSE close as the end of an active MCX session or stop its feed/exits. Set immutable data cutoffs for every job; exclude later observations from that job's training snapshot.

Run CPU/GPU-heavy training away from the order/exit loop with explicit resource/time budgets. If a deadline is missed, keep the eligible active model or the existing safe baseline. Persist the reason and make it visible. A model job must never delay risk-reducing execution.

### C. Record the information needed to learn correctly

For every decision and opportunity record the features available at the time, quote source/age, model and preprocessing versions, agent configuration, prediction probabilities, proposed action, risk rejection reason, order/fill identifiers and complete option-structure ID. Join decision -> intent -> order -> fill -> exit -> outcome reliably.

Store the action actually taken separately from the proposed action. Attribute returns to the chosen strategy and execution costs without pretending every agent caused the trade outcome. Preserve predictions as they were issued; never regenerate yesterday's predictions with today's weights and call them historical predictions.

Track data corrections with revisions. Never overwrite the evidence behind an already reported score. Separate true forward PAPER outcomes from historical replay, synthetic fixtures and LIVE records. Track source-mode differences and do not mix them as exchangeable observations without a justified method.

### D. Build labels only when outcomes are observable

At session close, open multi-day positions do not have final trade outcomes. Keep them pending. Update unrealized P&L as a separate monitoring measure; do not label them as final wins/losses or force an exit to manufacture a label.

For horizon-based forecasts, mature a label only after the complete prediction horizon and data-quality checks have elapsed. For example, a five-session prediction issued today cannot become a realized target tonight. For path-dependent barriers, require sufficient path data and record ambiguous/incomplete observations.

Match classification, return, volatility, fill-probability and slippage targets to their distinct objectives. Include correct lot multipliers and full costs when the label measures economic profitability. Score options at the complete-structure level when that is the decision unit. Do not duplicate one structure outcome across four legs and treat it as four independent successes.

Use eligible market observations for supervised forecasts, including periods when no trade occurred, to reduce policy-selection bias. Evaluate unexecuted opportunities with a clearly labelled historical simulation only when the required data exist. Do not invent actual fill outcomes for rejected orders or derive causal agent credit from observations alone.

### E. Separate daily evaluation from conditional retraining

Every session should produce data-health checks, reconciled P&L attribution, prediction scores on newly matured labels, drift indicators, and an agent review.

Retrain a candidate only when there are enough new independent labels, a configured maximum model age is reached, or a prespecified drift/underperformance trigger is met with adequate evidence. A losing day alone is not a sufficient retraining trigger. Permit a configured daily candidate build when enough data/resources exist, while preserving independent validation and promotion gates.

Train on a justified historical window plus newly eligible observations, not only the latest day. Compare rolling versus expanding windows and any recency weighting using training-period experiments. Bound the search and preserve older regimes sufficiently to test forgetting. Use incremental updates only for estimators that actually support them; otherwise perform a reproducible refit.

Configure per-model windows, label requirements, horizons, freshness limits, training cadence, drift thresholds, search budgets, training deadline and cooldown. Document their units and rationale. Do not invent one minimum sample threshold for every model or count dependent bars as independent evidence.

### F. Train the different model families appropriately

1. Return/direction models: test calibration and discrimination against simple baselines, then test cost-adjusted trading usefulness. Preserve the repository's existing statistical-noise acceptance threshold and rejection behavior. The absent return-forecaster artifact must remain absent unless a candidate earns acceptance.
2. Volatility models: use horizon-matched realized volatility targets, uncertainty/interval checks and simple volatility baselines. Verify that better forecasts improve executable option decisions after costs.
3. Meta-label models: predict the quality of a clearly defined base strategy's opportunities using information available before execution. Keep base-policy versions and selection effects explicit. Do not feed the completed trade's P&L or exit information into entry-time features.
4. Execution models: learn fill likelihood, slippage and rejection conditions from suitable order/quote observations. Evaluate calibration and economic consequences; censored, unfilled and cancelled orders need explicit treatment.
5. Reinforcement-learning policies, if actually implemented: train only in isolated replay/simulation with full execution and risk constraints; compare against a simple policy and validate reward/accounting. No online exploration with real funds, synthetic profit claims, or policy authority to change risk gates.

Do not enable every family by default. Mark models with missing data or unproven contribution as disabled/research. Persist acceptance evidence; rejected candidates must not be saved/loaded as serving artifacts, consistent with AGENTS.md. Retain rejection metrics and manifests without leaving a deployable rejected model behind.

### G. Define what agent learning means

Implement and report these as distinct mechanisms:
- Memory/reflection: store factual reviewed observations with source links and outcome confidence. Correct or retire disproven lessons rather than endlessly reinforcing them.
- Agent scoring: update bounded confidence calibration or advisory voting weights from sufficient linked observations, with shrinkage toward baseline and maximum per-session change limits.
- Prompt/configuration experiments: version candidate prompts/rules and evaluate them against a fixed benchmark and the existing agent configuration.
- Model-weight training: only claim LLM fine-tuning when a real supported training pipeline changed weights and produced independently validated artifacts. A new prompt or RAG entry is not LLM retraining.

Do not automatically fine-tune a local LLM nightly on a handful of trade summaries. If fine-tuning is investigated, require a suitable licensed/permissioned dataset, outcome quality, a supported training stack, resource estimates, an evaluation set, and evidence of incremental value. Keep this optional and offline if those prerequisites are absent.

Avoid circular feedback: do not train on the agent's own verdict as truth, reward an agent simply because it agreed with the final decision, or let a favourable generated narrative determine promotion. Account for correlated agents and disagreement. Use predeclared ablations/shadow comparisons to estimate contribution and label causal claims as unproven when appropriate.

Keep the active decision model and advisory weight/prompt bundle fixed for its declared session unless an explicit safety rollback is required. Intraday feedback may accumulate in pending state; it must not silently change the evaluated policy. Existing deterministic safety guards can still react immediately to losses or operational faults.

### H. Validate every candidate without information leakage

Fit preprocessing, imputation, normalization, feature selection and calibration only within the appropriate training partitions. Preserve time ordering; purge observations whose label intervals overlap evaluation boundaries and use a justified embargo when needed. A simple time-series split alone does not automatically solve all overlapping-label or cross-symbol leakage.

Evaluate champion, candidate and simple baseline under the same dates, information cutoffs, execution assumptions and risk/capital budgets. Report predictive scores, net expectancy, costs, drawdown, tail loss, turnover, exposure, calibration and uncertainty. Use dependent-data/structure-aware uncertainty estimates and record all tested candidates.

Prevent repeated nightly tuning against a supposedly untouched holdout. Record when evaluation evidence was consumed for selection. Separate reusable development validation from genuinely fresh forward observations and final holdouts. Preserve the reference model and comparison protocol so continual evaluation cannot silently turn test data into training targets.

Before unfreezing next-session PAPER eligibility, require both applicable model acceptance and economic/safety checks. A higher AUC alone is insufficient; a bigger profit obtained by taking more exposure is not matched-risk improvement. Thresholds must be configured and justified before evaluation. Missing evidence produces an explicit insufficient-evidence result, not PASS.

For leakage-prevention principles, consult the official scikit-learn documentation appropriate to the installed version:
https://scikit-learn.org/stable/common_pitfalls.html
https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html

### I. Promote and serve immutable, recoverable versions

Use immutable versioned bundles containing model weights when accepted, feature/preprocessing schemas, training/evaluation cutoffs, dataset hash, code revision, agent prompt/weight versions, cost schedule, instrument scope and complete gate outcomes. Select one registry authority rather than conflicting promotion states across services.

Write and validate a candidate bundle in isolation. Check checksum, supported schema, inference compatibility and deterministic replay before atomically switching a serving pointer. Keep last-known-good versions and test rollback. Open orders and exit plans retain their originating policy/version; changing the entry model must not orphan them or silently rewrite their risk management.

Research candidates may enter shadow evaluation. Automatic promotion to a future PAPER session is permitted only through an explicitly configured, tested policy after all required offline and forward evidence exists. It is not mandatory every day. LIVE promotion/arming remains a separate authorized process governed by all existing live invariants.

If the candidate fails, retain the champion only if the champion is still eligible. If no model is eligible, use the repository's documented baseline/no-trade behavior. Model-load or training failure must not cause arbitrary fallback to a rejected or unversioned artifact.

Evaluate newly matured forward outcomes continuously. Use documented, uncertainty-aware degradation criteria and operational integrity failures for rollback; avoid flip-flopping models after each loss. If all strategies are ineligible, allow no new trades while preserving valid exit management.

### J. Produce a daily learning report and next-session manifest

The report must show:
- Session/calendar/cutoff and data-quality status; reconciliation status.
- Gross/net P&L and costs; account and strategy/structure attribution.
- New mature labels, pending labels, rejected labels and effective sample limitations.
- Champion performance and calibration on predictions actually issued before outcomes.
- Candidate versions, training reason or skip reason, datasets and compute duration.
- Offline and shadow comparisons, cost stress, uncertainty and all gate decisions.
- Agent memory/weight/prompt changes, with evidence and bounded change amounts.
- Selected next-session bundle or retained baseline, and exact selection reason.
- Data/model drift, unresolved issues and rollback readiness.

Show technical status separately from economic improvement. Never label a job successful because a training function returned successfully when its candidate failed validation. Expose the report and manifest through authenticated API/dashboard views.

### K. Provide runnable commands and meaningful acceptance tests

Reuse existing entry points where possible; document any new commands. Provide dry-run, explicit session-date/as-of cutoff, resume, report, candidate-evaluate, and PAPER-serving-manifest modes. Dry runs must not mutate production stores or serving pointers. Keep retraining jobs out of broker-order processes and prevent overlap with existing schedulers.

Demonstrate the lifecycle on isolated historical/session replay with deterministic clocks. Test at least:
1. Session close with eligible completed labels produces a candidate/report.
2. Open multi-day trades and unexpired forecast horizons remain pending.
3. No trades/no new eligible labels produces a truthful skip/report and preserves serving.
4. Training-only preprocessing and purged label boundaries prevent future information leakage.
5. A candidate improving predictive scores but worsening net trading results is rejected.
6. A validation failure or artifact incompatibility cannot overwrite the active bundle.
7. Duplicate jobs, crashes and restarts cannot double-count learning or partially activate a bundle.
8. NSE and MCX session workflows coexist without stopping an active segment's data/exit services.
9. Resource timeouts and unavailable LLM/GPU leave execution responsive and serving recoverable.
10. Agent memory updates are labelled separately from model-weight training; agent changes cannot weaken safety gates.
11. Replaying the same cutoff reproduces the dataset and scores; later data cannot change that run silently.
12. Rollback preserves pending orders, exit plans, provenance and valid risk reductions.

Example expected operation:
During session T, version A serves decisions and records immutable predictions. After close, reconcile the session and score whichever earlier predictions now have mature outcomes. Add eligible labels to the approved dataset and build candidate B if justified. Evaluate B against A and the baseline using valid historical partitions; collect any required fresh shadow evidence subsequently. Session T+1 uses A unless B has already met the complete promotion policy. Otherwise B remains in shadow or is rejected. Five-session outcomes from T are added only when their horizons mature. This cycle can run daily without promising daily profit increases.

Deliver integrated source, scheduler/registry changes, schema migrations with dry runs, tests, CLI/runbook, API/UI reports, and a demonstration report. State accurately what training ran, what changed, what was rejected, and what future evidence is still needed. Do not claim that future sessions, retraining cycles, or profit improvements have already occurred.
