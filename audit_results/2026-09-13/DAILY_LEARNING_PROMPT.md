# Additional implementation prompt: session-end learning, training and retraining

Implement this specification together with `audit_results/2026-09-13/IMPLEMENTATION_PROMPT.md`. Preserve its safety, isolation, validation and deployment boundaries. This is a requirement to build and test the learning system; it does not authorize changing the running deployment or placing real-money orders.

Objective: after each completed trading session, analyze reliable observations, update eligible datasets, train/evaluate candidate models and agent configurations when justified, and prepare the best validated configuration for future PAPER sessions. Improve measured net expectancy and resilience where evidence supports it. Do not promise that retraining improves tomorrow's profit. Daily review must run; daily replacement of the active model must not be mandatory.

## A. Audit and integrate the existing learning paths

Inspect these starting points and follow their actual dependencies:
- `scripts/daily_scheduler.py`: already lists post-session review and daily/intraday retraining jobs.
- `scripts/train_return_forecaster.py`, `scripts/train_intraday_forecaster.py`.
- `scripts/scheduled_research_session.py` and the research harness.
- `trading_platform/neural/return_forecaster.py`, `intraday_forecaster.py`, `vol_forecaster.py`, `har_rv.py`, and serving code.
- `trading_platform/ai/meta_labeler.py` and the distinct `ai/ml_features/meta_labeling.py` implementation; establish which is trained ML and which is strategy scoring.
- `trading_platform/orchestrator/reflection_engine.py`, ProfitGuard, specialist crew and council supervisor.
- Trace, outcome-label, learning-journal, model registry and promotion services.

Document what each job currently does, what data it consumes, whether it changes weights/artifacts, and whether its output reaches serving. Repair missing integration instead of creating a duplicate scheduler or an unused trainer. Distinguish a model trained on market observations from a rule-based agent whose scoring weights or memory are updated.

## B. Implement a durable session-end workflow

Use an exchange-aware state machine, with durable run records:

SESSION_ACTIVE -> SESSION_CLOSED -> RECONCILED -> DATASET_READY -> EVALUATED -> TRAINED_OR_SKIPPED -> CANDIDATE_VALIDATED -> SHADOW_OR_PAPER_ELIGIBLE -> NEXT_SESSION_READY.

Also support explicit states for missing data, unresolved reconciliation, insufficient labels, validation rejection, resource timeout and operational failure. A completed review does not imply successful training or promotion.

Key each run by exchange/segment, trading-session date, dataset version and job version. Use a database lease or equivalent ownership mechanism, idempotent stages, bounded retries and checkpoints so duplicate scheduler processes/restarts cannot double-count labels, rewards or promotions. Support retrying one failed stage without rerunning completed mutations.

Trigger from the verified exchange calendar and observed session completion plus an ingest/reconciliation grace period. Keep the existing clock injectable in tests and use IST consistently. Support holidays and special sessions. Do not treat the NSE close as the end of an active MCX session or stop its feed/exits. Set immutable data cutoffs for every job; exclude later observations from that job's training snapshot.

Run CPU/GPU-heavy training away from the order/exit loop with explicit resource/time budgets. If a deadline is missed, keep the eligible active model or the existing safe baseline. Persist the reason and make it visible. A model job must never delay risk-reducing execution.

## C. Record the information needed to learn correctly

For every decision and opportunity record the features available at the time, quote source/age, model and preprocessing versions, agent configuration, prediction probabilities, proposed action, risk rejection reason, order/fill identifiers and complete option-structure ID. Join decision -> intent -> order -> fill -> exit -> outcome reliably.

Store the action actually taken separately from the proposed action. Attribute returns to the chosen strategy and execution costs without pretending every agent caused the trade outcome. Preserve predictions as they were issued; never regenerate yesterday's predictions with today's weights and call them historical predictions.

Track data corrections with revisions. Never overwrite the evidence behind an already reported score. Separate true forward PAPER outcomes from historical replay, synthetic fixtures and LIVE records. Track source-mode differences and do not mix them as exchangeable observations without a justified method.

## D. Build labels only when outcomes are observable

At session close, open multi-day positions do not have final trade outcomes. Keep them pending. Update unrealized P&L as a separate monitoring measure; do not label them as final wins/losses or force an exit to manufacture a label.

For horizon-based forecasts, mature a label only after the complete prediction horizon and data-quality checks have elapsed. For example, a five-session prediction issued today cannot become a realized target tonight. For path-dependent barriers, require sufficient path data and record ambiguous/incomplete observations.

Match classification, return, volatility, fill-probability and slippage targets to their distinct objectives. Include correct lot multipliers and full costs when the label measures economic profitability. Score options at the complete-structure level when that is the decision unit. Do not duplicate one structure outcome across four legs and treat it as four independent successes.

Use eligible market observations for supervised forecasts, including periods when no trade occurred, to reduce policy-selection bias. Evaluate unexecuted opportunities with a clearly labelled historical simulation only when the required data exist. Do not invent actual fill outcomes for rejected orders or derive causal agent credit from observations alone.

## E. Separate daily evaluation from conditional retraining

Every session should produce data-health checks, reconciled P&L attribution, prediction scores on newly matured labels, drift indicators, and an agent review.

Retrain a candidate only when there are enough new independent labels, a configured maximum model age is reached, or a prespecified drift/underperformance trigger is met with adequate evidence. A losing day alone is not a sufficient retraining trigger. Permit a configured daily candidate build when enough data/resources exist, while preserving independent validation and promotion gates.

Train on a justified historical window plus newly eligible observations, not only the latest day. Compare rolling versus expanding windows and any recency weighting using training-period experiments. Bound the search and preserve older regimes sufficiently to test forgetting. Use incremental updates only for estimators that actually support them; otherwise perform a reproducible refit.

Configure per-model windows, label requirements, horizons, freshness limits, training cadence, drift thresholds, search budgets, training deadline and cooldown. Document their units and rationale. Do not invent one minimum sample threshold for every model or count dependent bars as independent evidence.

## F. Train the different model families appropriately

1. Return/direction models: test calibration and discrimination against simple baselines, then test cost-adjusted trading usefulness. Preserve the repository's existing statistical-noise acceptance threshold and rejection behavior. The absent return-forecaster artifact must remain absent unless a candidate earns acceptance.
2. Volatility models: use horizon-matched realized volatility targets, uncertainty/interval checks and simple volatility baselines. Verify that better forecasts improve executable option decisions after costs.
3. Meta-label models: predict the quality of a clearly defined base strategy's opportunities using information available before execution. Keep base-policy versions and selection effects explicit. Do not feed the completed trade's P&L or exit information into entry-time features.
4. Execution models: learn fill likelihood, slippage and rejection conditions from suitable order/quote observations. Evaluate calibration and economic consequences; censored, unfilled and cancelled orders need explicit treatment.
5. Reinforcement-learning policies, if actually implemented: train only in isolated replay/simulation with full execution and risk constraints; compare against a simple policy and validate reward/accounting. No online exploration with real funds, synthetic profit claims, or policy authority to change risk gates.

Do not enable every family by default. Mark models with missing data or unproven contribution as disabled/research. Persist acceptance evidence; rejected candidates must not be saved/loaded as serving artifacts, consistent with AGENTS.md. Retain rejection metrics and manifests without leaving a deployable rejected model behind.

## G. Define what agent learning means

Implement and report these as distinct mechanisms:
- Memory/reflection: store factual reviewed observations with source links and outcome confidence. Correct or retire disproven lessons rather than endlessly reinforcing them.
- Agent scoring: update bounded confidence calibration or advisory voting weights from sufficient linked observations, with shrinkage toward baseline and maximum per-session change limits.
- Prompt/configuration experiments: version candidate prompts/rules and evaluate them against a fixed benchmark and the existing agent configuration.
- Model-weight training: only claim LLM fine-tuning when a real supported training pipeline changed weights and produced independently validated artifacts. A new prompt or RAG entry is not LLM retraining.

Do not automatically fine-tune a local LLM nightly on a handful of trade summaries. If fine-tuning is investigated, require a suitable licensed/permissioned dataset, outcome quality, a supported training stack, resource estimates, an evaluation set, and evidence of incremental value. Keep this optional and offline if those prerequisites are absent.

Avoid circular feedback: do not train on the agent's own verdict as truth, reward an agent simply because it agreed with the final decision, or let a favourable generated narrative determine promotion. Account for correlated agents and disagreement. Use predeclared ablations/shadow comparisons to estimate contribution and label causal claims as unproven when appropriate.

Keep the active decision model and advisory weight/prompt bundle fixed for its declared session unless an explicit safety rollback is required. Intraday feedback may accumulate in pending state; it must not silently change the evaluated policy. Existing deterministic safety guards can still react immediately to losses or operational faults.

## H. Validate every candidate without information leakage

Fit preprocessing, imputation, normalization, feature selection and calibration only within the appropriate training partitions. Preserve time ordering; purge observations whose label intervals overlap evaluation boundaries and use a justified embargo when needed. A simple time-series split alone does not automatically solve all overlapping-label or cross-symbol leakage.

Evaluate champion, candidate and simple baseline under the same dates, information cutoffs, execution assumptions and risk/capital budgets. Report predictive scores, net expectancy, costs, drawdown, tail loss, turnover, exposure, calibration and uncertainty. Use dependent-data/structure-aware uncertainty estimates and record all tested candidates.

Prevent repeated nightly tuning against a supposedly untouched holdout. Record when evaluation evidence was consumed for selection. Separate reusable development validation from genuinely fresh forward observations and final holdouts. Preserve the reference model and comparison protocol so continual evaluation cannot silently turn test data into training targets.

Before unfreezing next-session PAPER eligibility, require both applicable model acceptance and economic/safety checks. A higher AUC alone is insufficient; a bigger profit obtained by taking more exposure is not matched-risk improvement. Thresholds must be configured and justified before evaluation. Missing evidence produces an explicit insufficient-evidence result, not PASS.

For leakage-prevention principles, consult the official scikit-learn documentation appropriate to the installed version:
https://scikit-learn.org/stable/common_pitfalls.html
https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html

## I. Promote and serve immutable, recoverable versions

Use immutable versioned bundles containing model weights when accepted, feature/preprocessing schemas, training/evaluation cutoffs, dataset hash, code revision, agent prompt/weight versions, cost schedule, instrument scope and complete gate outcomes. Select one registry authority rather than conflicting promotion states across services.

Write and validate a candidate bundle in isolation. Check checksum, supported schema, inference compatibility and deterministic replay before atomically switching a serving pointer. Keep last-known-good versions and test rollback. Open orders and exit plans retain their originating policy/version; changing the entry model must not orphan them or silently rewrite their risk management.

Research candidates may enter shadow evaluation. Automatic promotion to a future PAPER session is permitted only through an explicitly configured, tested policy after all required offline and forward evidence exists. It is not mandatory every day. LIVE promotion/arming remains a separate authorized process governed by all existing live invariants.

If the candidate fails, retain the champion only if the champion is still eligible. If no model is eligible, use the repository's documented baseline/no-trade behavior. Model-load or training failure must not cause arbitrary fallback to a rejected or unversioned artifact.

Evaluate newly matured forward outcomes continuously. Use documented, uncertainty-aware degradation criteria and operational integrity failures for rollback; avoid flip-flopping models after each loss. If all strategies are ineligible, allow no new trades while preserving valid exit management.

## J. Produce a daily learning report and next-session manifest

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

## K. Provide runnable commands and meaningful acceptance tests

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
