# Implementation status — audit_results/2026-09-13/IMPLEMENTATION_PROMPT.md

Tracks work against the 2026-09-13 audit. Updated as each item is reproduced,
fixed, and tested. This file is the source of truth for what is actually
done vs. planned — never mark an item complete here without a passing test
proving it.

## Baseline

- Git revision at start of this work: `02d480be6e2377a21bcd8549fb8340d9e3c42127`
  (2026-09-13, "Persist the kill switch across restarts")
- Python 3.12.0, Node v24.18.0, npm 11.16.0
- Full backend suite at baseline: 1394 passed (per this session's own prior
  run, before touching audit findings — the audit's own 1,386/6-failed run
  used a different isolation wrapper; see its own note about the wrapper bug)
- `AGENTS.md` and `audit_results/` were untracked in the working tree when
  this work started (the audit tool created them but never committed) —
  committed as-is as the first step so the baseline is reproducible.
- Deployment: `trading-api` (port 8100), `trading-scheduler`, backed by
  PostgreSQL per the audit's `deployment-readonly.json`. This work targets
  isolated PAPER/BACKTEST/unit-test environments only, per the prompt's own
  boundary — no changes to the running deployment's data, and no redeploy
  without the same explicit review pattern used earlier this week (kill
  switch, scheduler fixes).

## Triage legend

- **CONFIRMED** — reproduced against current code with a failing test/script.
- **FIXED** — confirmed, then fixed, with a passing regression test.
- **ALREADY FIXED** — audit finding does not reproduce against current code.
- **NOT REPRODUCIBLE** — could not reproduce as described; noted why.
- **DEFERRED** — real, scoped, out of reach this pass (missing data/broker
  access/etc.) — recorded as an explicit dependency, not silently dropped.

## Findings (AUDIT_REPORT.md numbering)

| # | Finding | Severity | Status | Notes |
|---|---|---|---|---|
| 1 | opens_position=False bypasses kill switch on a flat account | Critical | **FIXED** | `domain/order_classification.py::derive_true_opens_position` verifies the caller's claim against real signed holdings at every enforcement point (scheduler kill-switch check + capital protection, RiskEngine.evaluate, runtime final gate). Verified against the audit's own `probes.py`: `actual_fills: 0`, `position_quantity: 0`, `opens_position` correctly overridden to `true`. New tests: `tests/test_order_classification.py` (13 cases incl. the exact audit scenario), `tests/test_runtime.py` (3 cases for the related finding #6 fix). Full suite: 1410 passed. |
| 2 | Deployed API auth disabled, published on all interfaces | High | **partially fixed** | `require_auth()`/`verify_token()` were already correct and already tested (an explicit `API_AUTH_REQUIRED=true` with an empty token already fails closed with 503) — the gap was that the insecure bypass was completely silent. Added: a `logging.CRITICAL` startup warning in `api/app.py` when auth is disabled, and an `api_auth_enabled` field on `GET /account/status` so it's visible to the operator, not just discoverable by an attacker's first unauthenticated probe. **Asked the user explicitly (2026-09-13): declined for now** — "Not now, leave it as-is," keeping the current no-login convenience for this single-operator, Tailscale-only setup. The live deployment's `docker-compose.yml` still defaults `API_AUTH_REQUIRED` to `false` and publishes `0.0.0.0:8100`; this is now a documented, visible, deliberate choice rather than a silent gap — revisit if the deployment's network exposure model changes. Tests: `tests/test_runtime.py` (+1). Full suite: 1450 passed. |
| 3 | Outdated STT rates; short-vol charges use net credit not gross per-leg turnover | High | **FIXED** | Verified current rates via NSE Circular Ref. No. 02/2026 (Budget 2026 hike, effective 2026-04-01): futures STT sell-side 0.02%→0.05%, options STT sell-premium 0.10%→0.15% (old code had 0.0125%/0.0625% — wrong for both eras). `ChargesModel.estimate()` gained a date-effective `as_of` param (both live call sites now pass the real fill date) so a future replay spanning the hike uses the rate that actually applied. Separately, `short_vol_backtest.py::_charges()` was handed a condor's NET credit/debit and taxed the whole thing as one sell-side turnover; new `_leg_turnovers()` prices each leg individually (reusing the existing Black-Scholes/smile-IV pricer) and tracks entry+exit gross sell/buy turnover per leg side, which also let the `is_long_vol` branch-specific charge-argument-swapping be deleted (the same formula is now correct for every structure). Tests: `tests/test_charges_model.py` (7 cases), `tests/test_short_vol_backtest_charges.py` (5 cases). Full suite: 1439 passed. |
| 4 | Backtest metrics drop lot multiplier (₹500 ledger vs ₹10 metric) | High | **FIXED** | `Trade` gained a `lot_size: int = 1` field (default preserves every existing call site); `PortfolioLedger.apply_fill` (the only construction site) now always passes the real `instrument.lot_size`. `round_trip_pnls()` multiplies by the closing trade's `lot_size` (mirroring how the ledger's own realized_pnl always reads it fresh at the closing fill); charges are not multiplied (already in rupees). Also persisted to the `trades` table (additive migration) for reconciliation — **known limitation**: pre-migration historical rows backfill to 1, wrong for any historical derivative trade; flagged in the migration comment, not silently trusted. Verified against the audit's own `probes.py`: ledger PnL 500 now matches `round_trip_pnls` output exactly (was 10). Tests: `tests/test_backtest_metrics.py` (5 cases). Full suite: 1415 passed. |
| 5 | Generic backtester: lookahead, option/underlying price mixing, holding-period bugs | High | **FIXED (3 of the cited defects)** | (1) Entry fill price for OPTIONS used the raw underlying's own bar-open price directly (e.g. NIFTY ~24000 as an option's premium) — now routed through the existing `_mark_price_for_instrument` Black-Scholes pricer (previously only used for marking, never for pricing the fill itself); non-options unaffected. (2) An option's intrabar stop/target check compared a premium-denominated level against the UNDERLYING's own raw OHLC (incompatible scales) — now prices the option at both of the underlying's intrabar extremes and takes min/max, correctly bracketing the option's own implied range for calls and puts alike. (3) The daily-loss circuit breaker's "session-start equity" was computed from THIS bar's own CLOSE (not known until end of day) and mislabeled "bar_open" — now uses each underlying's own OPEN price, the only information genuinely available before the bar's decisions are made. (4, bonus finding from investigating this) every strategy registered in this engine holds >= 2 days (`exit_rules().max_holding_days`, verified none use 1) but was tagged `ProductType.INTRADAY` — `ChargesModel` branches on this for equity STT/brokerage, so multi-day holds were charged the wrong (lower) rate; now tagged `CARRYFORWARD`, confirmed via `grep` that no other risk/scheduler code branches on `product_type` so this is isolated to charges. **Not done**: the audit's exit-processed-before-entry ordering point was reviewed and judged an inherent, disclosed limitation of daily-bar granularity (no same-day entry+exit without intrabar sequencing this engine's OHLC data can't provide), not a fixable bug — left as-is. Given this generic engine gates no real capital (short_vol's own dedicated backtest is separate and already validated per finding #9), this fix improves diagnostic accuracy, not live safety. Tests: `tests/test_backtesting.py` (+4). Full suite: 1454 passed. |
| 6 | LIVE emergency reduction can be blocked by overall readiness during kill switch | High | **FIXED** | Fixed alongside #1 (same function): a verified reduction (per the finding-#1 derivation, not the caller's claim) may bypass ONLY the `kill_switch` readiness gate; every other gate (execution mode, live flag, confirmation phrase, broker credentials, instrument freshness, feed staleness) still fully applies. Tests: `test_live_reduction_bypasses_only_the_kill_switch_readiness_gate`, `test_live_entry_still_blocked_by_kill_switch_readiness_gate`, `test_forged_reduction_on_flat_symbol_still_blocked_in_live_gate` (tests/test_runtime.py). |
| 7 | Session-loss baseline uses cost-basis equity, resets on restart | High | **FIXED** | New `session_baselines` table (one row per IST trading date) + `TradingRuntime._establish_session_baseline()`: the premarket routine (`force_fresh=True`) always computes a fresh MARKED (not cost-basis) equity via the same price-resolution chain `/portfolio/positions` uses, and persists it; process startup (`force_fresh=False`) restores today's persisted value if one exists (a mid-session restart can no longer silently recompute/reset it), computing fresh only on a genuine cold boot with nothing persisted yet. Tests: `tests/test_session_baseline.py` (5 cases, including the exact restart-preserves-this-morning's-baseline regression). Full suite: 1427 passed. |
| 8 | Exchange holiday calendar wrong (Holi, Sept 14, Diwali, others) | High | **FIXED** | Corrected the 2026 `_HOLIDAYS` set in `agent/market_hours.py`, cross-verified against two independent sources that agree exactly (NSE's own circular PDF timed out on fetch — re-verify against it and the holiday-notice page when reachable, both cited in the module comment). Fixed: Holi (was 25 Mar, actually 3 Mar), Diwali (was "4 Nov Laxmi Puja" — wrong date AND wrong holiday name; correct is "10 Nov Diwali-Balipratipada" — 8 Nov is a Sunday special Muhurat session, not a closure). Added 6 previously-missing holidays: Ram Navami, Mahavir Jayanti, Bakri Id, Muharram, Ganesh Chaturthi (Sept 14 — the single most urgent fix, literally the next day), Dussehra, Guru Nanak Jayanti. 2027 entries left as-is and still marked tentative — NSE has not published that circular yet. Tests: `tests/test_market_holidays_2026.py` (7 cases). Full suite: 1422 passed. **Not yet done**: sections 5/21's broader ask for a versioned, date-effective, expiry-adjustment-aware calendar system — this fix corrects the data, not the architecture. |
| 9 | short_vol validation narrower than deployed config, cost-sensitive | High | **FIXED (binding) + major real finding** | `run_short_vol_gates.py` persisted every underlying under the SAME `strategy_id="short_vol"` with no `git_sha` — validating NIFTY alone made the promotion ladder consider short_vol "cleared" for every underlying the live executor is configured for. The live `.env`'s `SHORTVOL_AUTO_UNDERLYINGS=NIFTY,BANKNIFTY,FINNIFTY,CRUDEOIL,NATURALGAS,GOLD,SILVER` — 6 of 7 actively-trading underlyings had NEVER been independently validated (code's own default is NIFTY-only). Fixed: `strategy_id` is now per-underlying (`short_vol_<name>`), `git_sha` is captured, `config_snapshot` records the actual winning variant's parameters + explicit data-provenance disclosure + selection-method caveat (best-of-sweep-on-same-data, not an untouched holdout). **Then actually ran it** for the 2 underlyings with cached data (BANKNIFTY, FINNIFTY) — real, important findings: **BANKNIFTY fails its DSR gate** (0.0856 vs required 0.5) despite modest positive backtested P&L; **FINNIFTY fails every single gate** (DSR, PBO, Monte-Carlo drawdown) with the best-of-27-variants sweep itself net NEGATIVE (-₹105,596 / -1.50%/yr over 7.4y). NIFTY re-persisted under the new binding, still passes cleanly (+₹281,554, 3.41%/yr). All 3 results persisted to the local dev DB (not the live deployment's Postgres — did not touch the running system). **Remaining, explicit dependency**: CRUDEOIL/NATURALGAS/GOLD/SILVER have no cached historical data (`data/historical/*.csv` missing) — fetching it needs a live Angel One data pull (credentials are configured but this wasn't run, to avoid an uncoordinated live API call); these 4 commodities remain completely unvalidated. **Asked the user explicitly (2026-09-13)**: "Leave both running for now" — revisit once CRUDEOIL/NATURALGAS/GOLD/SILVER are also validated so the decision covers all 7 configured underlyings at once, rather than pulling BANKNIFTY/FINNIFTY piecemeal. Full suite: 1450 passed. |
| 10 | Dashboard strategy filter field mismatch; synthetic 2-point equity curve | Medium | **FIXED** | Backend: `BacktestResult.to_dict()` now exposes the real per-bar `equity_curve`/`trade_pnls`/`total_charges` it already computed but never sent, plus a `data_source` field (always `"synthetic"` today — `BacktestEngine` never receives a real `HistoricalDataProvider`) and a real per-fill `fills` log (filtered from `reports`, which itself was never actually sent despite the frontend's dead code expecting it). Frontend: fixed `strategy_name`→`strategy_names` (extracted `buildBacktestPayload()` as a testable pure function), wired the real equity curve into the chart, added a visible "Synthetic prices" badge, replaced the dead trade-log section (never rendered — read a field the backend never sent) with a real one reading `fills`. Tests: `tests/test_runtime.py` (+3 cases), `hft_frontend/tests/backtest_payload.test.ts` (+3 cases). Full suite: 1441 passed; frontend 7/7 passed; `npm run build` clean. **Not yet done**: this endpoint still has no real historical option-chain data source (section 6's ask) — `data_source` now at least reports that honestly instead of implying real replay. |
| 11 | Trace joins broken (0 joined observations); promotion reports 0 paper days | Medium | **FIXED** (paper days) / **explained, not a bug** (trace joins) | **Paper days**: `StrategyPromotionService` was never given a `paper_days_lookup` (confirmed at `runtime.py`'s construction site), so it always fell back to `record.metadata.get("paper_days", 0)`, a key nothing writes — new `TradingDatabase.distinct_paper_trading_days()` derives it from the immutable trades table (COUNT DISTINCT calendar date, PAPER mode, excluding test rows), now wired in. **Trace joins**: traced conclusively — this is NOT a fixable ID-propagation bug. `MasterOrchestrator.run()` (the council-voting cycle) runs every scan and votes on directional-momentum candidates, but those are only ever enqueued when `AGENT_DIRECTIONAL_ENABLED=true` (off by default, no proven edge); `short_vol_executor.py`, which generates every real fill in this deployment, never calls `MasterOrchestrator` or records any `agent_outputs` at all. The council's votes and the trades that actually execute are two disjoint populations by construction — no trace-ID resolution can join votes never cast on decisions that never happened. Corrected `evaluate_council_skill()`'s own diagnostic message (previously misleadingly implied a simple ID-namespace fix) to name the real cause and the two real options (enable directional trading, or route short_vol through an advisory-only council check) as product decisions, not bugs. Tests: `tests/test_validation_gates.py` (+7: paper-days lookup + DB method), `tests/test_council_skill_eval.py` (+1: message content). Full suite: 1449 passed. |

Sections 12-21 of the implementation prompt (execution realism, attribution,
research backlog, ML/LLM validation, promotion, daily learning pipeline) are
large R&D programs, not single findings — tracked separately below once the
numbered findings above are through triage, per the prompt's own dependency
ordering ("complete mandatory correctness/security/data work before
interpreting strategy improvements").

## Numbered findings: final tally

All 11 numbered findings triaged. 8 fully fixed and tested (1, 3, 4, 6, 7,
8, 10, 11). 1 fixed for the code/observability part with the live-config
decision explicitly deferred by the user (2). 1 fixed for the evidence-
binding part with two live-trading decisions explicitly deferred by the
user pending full-universe validation (9). 1 fixed for 3 of its cited
defects, 1 reviewed and judged not a bug (5). Every fix has a regression
test and the full backend suite (1454 tests) plus frontend build/tests
were green after each one. Nothing here was merged into main without
running the suite first.

## Section 6/8 (data quality, execution realism) — started

Triggered by a real live incident on 2026-09-14 (Ganesh Chaturthi — see
memory `ganesh-chaturthi-holiday-incident-2026-09-14.md`): the calendar fix
hadn't been deployed before the date it protected, and a live-feed
auto-start gap let the feed run on a real holiday, producing a corrupted
quote (~18x the strategy's own signal price) that a fill used verbatim,
tripping the kill switch on a data-integrity artifact rather than a market
loss. Both immediate triggers are fixed (commits `1fd783d`, `c753a5a`,
deployed and verified), but the underlying "trust any live tick
unconditionally" gap in `SimulatedBrokerClient._live_price()` was still
live — fixed as its own, more general hardening:

- **Implausible price rejection** (section 8): a live tick more than 5x
  above or 1/5 below the strategy's own signal price for the same
  instrument is rejected and the fill falls back to the signal price
  instead — logged and recorded via `note_swallowed`, not silent. Wide
  enough that real volatility (options doubling/tripling) is never
  rejected. Applies to every fill path including the resting-limit
  `order_status` poll.
- **Staleness rejection** (section 6 — "detect stale... quotes... assess
  freshness for each traded option leg"): a tick older than 5 minutes is
  similarly rejected. Missing timestamp metadata (an adapter/test double
  that doesn't carry one) is not itself grounds for rejection — only a
  confirmed stale age is.

Tests: `tests/test_blocking_fixes.py` (+12 across both checks). Full suite:
1465 passed. Deployed and verified live (redeployed trading-api, confirmed
healthy, kill switch stayed clear).

**Not done**: the much larger remaining scope of sections 6/7/8 —
exchange-vs-receive-vs-decision timestamps as distinct recorded fields,
sequence-gap/duplicate/crossed-book detection, corporate-action/symbol-
change point-in-time validation, and a real historical option-chain data
source — are architecture-level programs, not follow-on patches to this
one incident.

## Sections 12-21 (implementation prompt) — status

**Not started.** These are not individual bugs — they are large, multi-week
research/engineering programs in their own right (execution realism and
order-lifecycle idempotency, per-rupee performance attribution, entry-
quality and frequency research, volatility modelling, exit/tail-risk
research, portfolio concentration controls, regime/event-filter evaluation,
a falsifiable strategy research backlog, ML/LLM validation without leakage,
promotion/versioning of served models, and a full session-end daily-
learning state machine with its own 11-point acceptance-test list).
Attempting them in the same pass as the numbered findings above would mean
either rushing them (producing exactly the kind of "fake edge presented as
intelligence" this project's own honesty discipline exists to prevent) or
not finishing the numbered findings properly first, which section 20 of the
prompt itself says to avoid ("Complete mandatory correctness/security/data
work before interpreting strategy improvements").

Recommended next steps, in the prompt's own dependency order:
1. Section 6 (data quality/provenance) and section 8 (execution realism) —
   natural next since sections 1-5 (accounting/costs/calendar/backtester)
   are now settled and these consume that work directly.
2. Section 21 (daily learning pipeline) — the prompt's own §21A asks to
   first *audit* the existing scheduler jobs, training scripts, and
   learning-journal/trace infrastructure before building anything new;
   several of the memory files in this session already describe pieces of
   this (research/hypothesis_harness.py, the LLM research loop,
   strategy_validator.py's registry) that a real audit pass should
   reconcile against §21's state-machine design rather than duplicate.
3. Sections 9-17 (research, ML validation) — genuinely require real
   option-chain data and time-series compute this pass didn't have
   authorization or scope to run; §15's "select at most three initial
   experiments" is the right-sized unit of work for a future session, not
   all of them at once.

## Section 21A — audit of the existing daily-learning infrastructure (2026-09-14)

Per the prompt's own explicit instruction, this ran *before* any new pipeline
code: "Document what each job currently does, what data it consumes, whether
it changes weights/artifacts, and whether its output reaches serving. Repair
missing integration instead of creating a duplicate scheduler or an unused
trainer."

### Job-by-job

| Job (scripts/daily_scheduler.py) | Data | Changes weights/artifacts? | Reaches serving? |
|---|---|---|---|
| `daily_ai_review` (15:40) | Local LLM review of the day's trades | No — writes `status='pending'` suggestions only | N/A by design, advisory only |
| `model_retrain` (16:15) | Real Angel One daily candles, or cached CSV; never synthetic (deliberate — see docstring) | Yes, on a validated horizon: `winner.save(MODEL_PATH)` | **No — see finding below.** Now fixed. |
| `intraday_retrain` (16:30) | Real 5-min candles, cached-symbols-only when Angel One isn't configured | Yes, on a validated horizon: same `save()` pattern | **No — same root cause.** Now fixed. |
| `ReflectionEngine` (agent council voting weights, `orchestrator/reflection_engine.py`) | Every resolved trade, via `reflect()` | Yes — in-process EMA update, persisted via `set_db()`/`save`, restored at startup via `load_weights_from_db()` (`runtime.py:493-494`) | Yes — same object instance serves and learns, no cross-process handoff. Confirmed correctly wired. |
| `MetaLabeler` champion/challenger (`trading_platform/ai/meta_labeler.py`) | Every fill, via `_on_fill` → `record_outcome()` | Yes — in-process EMA per (strategy, regime) | Yes — `champion_strategies()` reads the same in-memory state. No gap. |
| `HAR_RVForecaster` / GARCH (vol forecasting used by short_vol when `SHORTVOL_USE_VOL_FORECAST=true`) | Live ticks/bars fed via `.update()` | Yes — online OLS refit, in-process | Yes — self-contained, no separate train-then-load cycle, so this class of bug structurally can't occur here. |

### Finding: nightly model retrains could never reach the live process (fixed)

Two compounding bugs meant `model_retrain`/`intraday_retrain` were completely
disconnected from what `trading-api` actually serves — worse than the
"needs a restart" the jobs' own log lines claimed:

1. **`docker-compose.yml`'s `scheduler` service never mounted `./models`**
   (only `./data`), while `trading-api` does (`./models:/app/models`,
   line 71). `MODEL_PATH` in both `scripts/train_return_forecaster.py` and
   `scripts/train_intraday_forecaster.py` resolves to `/app/models/...`
   inside the container. Without the mount, `winner.save(...)` wrote into
   the **scheduler container's own ephemeral layer** — the file never
   reached the host `./models` directory, so no restart of `trading-api`,
   however performed, could ever pick it up. A scheduler container
   rebuild/redeploy (this happened twice earlier in this same session)
   would silently destroy any such artifact.
2. **`NeuralPredictionService` only ever loaded forecaster artifacts once**,
   at construction (`trading_platform/neural/serving.py:300-308`), and
   `TradingRuntime.__init__` constructs it exactly once, at process startup
   (`runtime.py:445-449`). No reload endpoint, periodic re-check, or
   file-watcher existed anywhere (confirmed by grep — the only "reload"
   hits in the whole runtime are `TraceStore`'s own unrelated startup
   reload). `GradientBoostedReturnForecaster.load()` (`return_forecaster.py:381`)
   is a plain eager `joblib.load` into `self._model`, not a lazy/mtime-aware
   loader.

Net effect: even a genuinely validated, promoted model would sit on disk
(or not even that, per #1) forever, while the live process kept serving the
MA baseline — with no operator-visible signal that "promoted" didn't mean
"live." This has not caused any wrong live decision to date, because every
real run of both jobs has correctly found no OOS edge and declined to
promote anything (daily-bar TA: AUC≈0.50; intraday as of 2026-07-26:
0.4934-0.4970 vs a 0.5200 threshold) — `models/return_forecaster*` and
`models/intraday_forecaster*` don't exist on disk today. This was a latent
landmine for the day the research actually pays off, not an active loss.

**Fix** (repair-in-place, not a new pipeline):
- `docker-compose.yml`: added `./models:/app/models` to the `scheduler`
  service's volumes.
- `TradingRuntime.reload_neural_models()` (`runtime.py`): rebuilds
  `self._neural_service` from the current `./models` files and swaps it in.
- `POST /neural/reload-models` (`app.py`, auth-gated like the existing
  `/neural/predict-preview`): exposes it.
- `scripts/daily_scheduler.py`: `_reload_neural_models()` helper calls that
  endpoint (via the same `_api_post` retry/auth pattern `eod_square_off`
  already uses) right after `winner.save(...)` in both `model_retrain` and
  `intraday_retrain`; a failed call logs a warning and explicitly states the
  old model keeps serving until a manual restart, rather than silently
  claiming success.

Tests: `tests/test_runtime.py::test_reload_neural_models_is_a_noop_when_neural_lab_disabled`,
`::test_reload_neural_models_swaps_in_a_freshly_loaded_service`. Full suite:
1467 passed.

Deployed and verified live (2026-09-14, a non-trading day — Ganesh
Chaturthi): rebuilt and recreated `trading-api` and `trading-scheduler`;
`docker inspect trading-scheduler` confirms the new `./models:/app/models`
mount; `trading-api` reports `healthy`; `POST /neural/reload-models`
returns `{"reloaded": true, "before": {...}, "after": {...}}` — both
forecasters correctly report `available: false` since neither has ever
validated on real data yet, matching the "no artifact exists on disk"
state noted above.

### Finding: two same-purpose-sounding "meta-labeling" modules — one live, one fully dead code

- `trading_platform/ai/meta_labeler.py::MetaLabeler` — real, live, in-process
  champion/challenger EMA scorer. Correctly wired (see table above).
- `trading_platform/ai/ml_features/meta_labeling.py::MetaLabelingConfig` +
  its LightGBM pipeline — a complete, well-documented, walk-forward-gated
  signal-filtering classifier (conformal prediction, isotonic calibration,
  the platform's own deployment law spelled out in its docstring). Grepping
  the entire tree for its class names turns up **zero callers anywhere**,
  including its own tests.

This is exactly the "unused trainer" the prompt's own audit language warns
against building a duplicate of — one already exists, fully built, sitting
inert. Not wiring it in now: doing so would mean putting an unvalidated
LightGBM signal filter into the live decision path with no walk-forward run
against real data backing it up, which is the "rushing it" trap section 20
already warned about. Recorded here so it isn't mistaken for active
infrastructure, and so a future session either validates and wires it
properly or removes it — leaving inert ML code around unlabeled is itself
a form of the "fake intelligence" this project's honesty discipline exists
to prevent.

### Confirmed healthy, no action needed
`ReflectionEngine`, `MetaLabeler` (the real one), `HAR_RVForecaster`/GARCH —
all in-process, self-updating, no cross-process artifact handoff, so this
class of bug structurally cannot occur for them (see table above).
`run_daily_ai_review` is advisory-only by design (writes `status='pending'`,
never auto-applies) — not a gap, working as documented.

### Explicitly out of scope for this pass
`RegimeClassifier`/`MetaModel` (`runtime.py:172-178`, loaded once at startup
gated by `settings.auto_load_models`) have the same "loaded once at
construction" shape, but no scheduled job in `daily_scheduler.py` (or
anywhere else) retrains them — whatever writes `models/regime_classifier*`/
`models/meta_model.json` today is a manual/on-demand path via
`api/regime_meta_service.py`, not part of the *daily* learning pipeline this
section audits. Flagged for a future pass if/when that becomes automated.

The rest of Section 21 (B-K: durable session-end workflow, label-maturation
rules, a daily learning report, promotion/versioning beyond what
`StrategyPromotionService`/`strategy_validator.py` already do) remains
unstarted — it is new-architecture work, not integration repair, and per
this section's own findings there is no evidence yet that the existing
pieces need replacing rather than the specific connective-tissue gap fixed
above.

## Section 21D — audit: are labels only built when outcomes are observable?

Checked `trading_platform/trace/label_factory.py::OutcomeFactory` (the only
place trade outcome labels are computed) against §21D's requirement that
open multi-day positions must stay pending, never get a manufactured label.

**Compliant by construction.** `compute_exit_label()` is the only place an
`OutcomeLabel` is created, and it does `self._pending.pop(symbol, None)` —
if no exit fill has happened yet it returns `None` and the entry simply
stays in `_pending` (exposed via `pending_symbols()`). Confirmed via grep
that the only production caller is `runtime.py`'s `_on_fill`, gated on
`intent.priority != OrderPriority.ENTRY` — i.e. a genuine exit fill, never
a session-close snapshot or synthetic mark. No EOD/session-close code path
calls `compute_exit_label()` at all.

**Real gap found**: §21D also asks that horizon-based *forecast* labels
("a five-session prediction issued today cannot become a realized target
tonight") mature only after the full horizon elapses. No such mechanism
exists anywhere — there is no code that logs a live forecast with its
horizon and later, once enough sessions have passed, scores it against the
realized outcome. `return_forecaster.py`/`intraday_forecaster.py`'s own
validation is entirely offline (walk-forward backtest at training time);
nothing scores predictions the running process actually issued live. Not
fixed this pass — recorded as a genuine, scoped gap for a future session
(needs a small forecast-log table + a scheduled maturity-check job, not a
full rebuild).

## Section 21J — daily learning report (built)

`model_runs` (written by `daily_scheduler.py`'s retrain jobs via
`save_model_run()`) had **no matching read method anywhere** — confirmed by
grep; it was write-only from the application's own perspective. Every
night's verdict (accepted/rejected, per horizon, with AUC) was durably
recorded and permanently invisible to any API, dashboard, or human without
manual SQL. This alone is worth fixing regardless of the rest of §21J's
much larger ambition.

Added:
- `TradingDatabase.recent_model_runs(model_name=None, since=None, limit=200)`
  — dual-dialect read (SQLite/Postgres), reuses the existing `_json_field`
  JSONB-vs-TEXT decode helper for the `metadata` column.
- `TradingRuntime.daily_learning_report(as_of: date | None = None)` —
  aggregates today's model-retrain verdicts, `daily_pnl` row, realized-label
  stats (`OutcomeFactory`), agent voting weights (`ReflectionEngine.stats()`),
  champion/challenger state (`MetaLabeler.summary()`), neural-forecaster
  availability, and strategy-promotion state
  (`StrategyPromotionService.list_promotions()`).
- `GET /learning/daily-report` (auth-gated, optional `?session_date=`).

**Deliberately not a full §21J implementation.** Several of the spec's
bullets have no real backing data yet — rather than fabricate them, each
section of the report carries an explicit `spec_coverage` string stating
covered / partial / not_built and why, matching this project's honesty
discipline (CLAUDE.md: never present a stub as intelligence). Concretely
not_built: shadow/offline candidate comparison, cost-stress and uncertainty
reporting, data/model drift detection, per-rupee gross-vs-cost attribution,
and an immutable versioned-bundle registry with checksums/rollback (§21I).
These remain open for a future, properly-scoped session — most need real
option-chain data or a validated model to have anything genuine to report
on, neither of which exists yet in this deployment.

Tests: `tests/test_runtime.py::test_daily_learning_report_reads_back_todays_model_runs`,
`::test_daily_learning_report_has_required_top_level_sections`. Full suite:
1469 passed.

## Section 21B — bounded time budget for training jobs (fixed)

`scripts/daily_scheduler.py`'s main loop is single-threaded and sequential
with no timeout anywhere around a job call. `model_retrain` (16:15) and
`intraday_retrain` (16:30) do real local compute (an Angel One candle fetch
through the third-party SmartConnect SDK + a sklearn fit across up to 12
symbols / 4 horizons), unlike every other job, which only makes a
short-timeout HTTP call via `_api_post`. A hang in either — most plausibly
a network stall inside the SDK, which this codebase doesn't control —
would silently starve every job scheduled later that day, including
`mcx_eod_square_off` at 23:25: a genuine risk-reducing close-out for
commodity positions, not merely a stale model. This matches §21B almost
verbatim: "Run CPU/GPU-heavy training away from the order/exit loop with
explicit resource/time budgets ... A model job must never delay
risk-reducing execution."

Added `_run_job_with_budget()`: runs the job in a daemon thread and joins
with a 30-minute budget (generous over the normal few-minute runtime,
while guaranteeing the loop is free again hours before 23:25). If the
budget is exceeded the main loop moves on instead of blocking; a
re-entrancy guard prevents a second invocation from starting a concurrent
second trainer if the first is still running past its budget. Scoped only
to the two training jobs.

Tests: `tests/test_daily_scheduler.py::JobTimeBudgetTests` (4 new). Full
suite: 1473 passed. Deployed live (both containers rebuilt from the same
commit, per this session's own established practice of not letting the
two images drift — see the Ganesh Chaturthi incident).

## Section 21D — audit: are labels only built when outcomes are observable?

Checked `trading_platform/trace/label_factory.py::OutcomeFactory` (the only
place trade outcome labels are computed) against §21D's requirement that
open multi-day positions must stay pending, never get a manufactured label.

**Compliant by construction.** `compute_exit_label()` is the only place an
`OutcomeLabel` is created, and it does `self._pending.pop(symbol, None)` —
if no exit fill has happened yet it returns `None` and the entry simply
stays in `_pending` (exposed via `pending_symbols()`). Confirmed via grep
that the only production caller is `runtime.py`'s `_on_fill`, gated on
`intent.priority != OrderPriority.ENTRY` — i.e. a genuine exit fill, never
a session-close snapshot or synthetic mark. No EOD/session-close code path
calls `compute_exit_label()` at all.

**Real gap found**: §21D also asks that horizon-based *forecast* labels
("a five-session prediction issued today cannot become a realized target
tonight") mature only after the full horizon elapses. No such mechanism
exists anywhere — there is no code that logs a live forecast with its
horizon and later, once enough sessions have passed, scores it against the
realized outcome. `return_forecaster.py`/`intraday_forecaster.py`'s own
validation is entirely offline (walk-forward backtest at training time);
nothing scores predictions the running process actually issued live. Not
fixed this pass — recorded as a genuine, scoped gap for a future session
(needs a small forecast-log table + a scheduled maturity-check job, not a
full rebuild).

## Section 21H — audit: leakage-safe validation (real bug found and fixed)

Checked `return_forecaster.py`/`intraday_forecaster.py`'s walk-forward
validation against §21H: "Preserve time ordering; purge observations whose
label intervals overlap evaluation boundaries ... A simple time-series
split alone does not automatically solve all overlapping-label leakage."

The split itself IS a proper expanding-window walk-forward (`X[:train_end]`
train, `X[train_end:test_end]` test, strictly chronological, no shuffling)
— time ordering is preserved. But `_build_rows()` computes each row `i`'s
label from `closes[i + horizon]` — a forward-looking horizon. Without a
purge, the training rows immediately before a fold boundary (`train_end -
horizon` through `train_end - 1`) have labels computed from prices at or
past `train_end` — i.e. inside the test block. This is a real overlapping-
label leak, present in BOTH forecasters (identical code shape), for every
horizon greater than 1 — which includes 3 of the 4 swept horizons in
`RETRAIN_HORIZONS`/`INTRADAY_HORIZONS`.

**Fix**: `_walk_forward_metrics()` in both files gained an `embargo`
parameter; each fold's training slice is now `X[:train_end - embargo]`
instead of `X[:train_end]` (the test slice is unchanged — only the
contaminated training tail is cut). `_fit_validate()` in both classes now
passes `embargo=self._horizon`. For the currently-used `DEFAULT_HORIZON=1`
the practical effect is a 1-row purge per fold (negligible); the fix
matters for the sweep's larger horizons (3/5/10), where it removes a
real, previously-unaccounted-for source of optimistic bias in the walk-
forward AUC estimate.

Tests: `tests/test_return_forecaster.py::test_walk_forward_embargo_purges_the_contaminated_training_tail`,
`tests/test_intraday_forecaster.py::test_walk_forward_embargo_purges_the_contaminated_training_tail`
— both assert the exact purged training-set size per fold via a recording
fake model, independent of stochastic classifier behavior. Full suite:
1475 passed.

## Sections 21E, F, G, I, K and the full §21B state machine — audited, not built

- **21E** (separate evaluation from conditional retraining): the current
  "daily candidate build" (unconditional nightly sweep + independent
  validation/promotion gate) is one of §21E's two explicitly *permitted*
  modes ("Permit a configured daily candidate build when enough
  data/resources exist, while preserving independent validation and
  promotion gates") — compliant. Not compliant: the per-model windows/
  horizons/thresholds are hardcoded constants, not centrally configured
  with documented units/rationale as §21E asks. Left as-is; a config
  surface for these is real but lower-value work (nothing currently
  depends on tuning them per-model).
- **21F** (train model families appropriately): return/intraday direction
  models already match 21F.1's requirements (baseline comparison,
  cost-adjusted usefulness is implicit in the acceptance gate, absent
  artifact stays absent). No meta-label ML model, execution model, or RL
  policy is enabled by default — compliant with "do not enable every
  family by default."
- **21G** (agent learning definitions): `ReflectionEngine` already
  implements bounded EMA agent-weight updates with shrinkage (see its own
  `WEIGHT_MIN`/`WEIGHT_MAX`/`EMA_ALPHA`) — matches 21G's "agent scoring"
  definition. No LLM fine-tuning exists anywhere (correct — 21G explicitly
  prohibits nightly fine-tuning on a handful of trade summaries).
- **21I** (immutable versioned promotion registry) — **real gap found and
  partially fixed**: `NeuralPredictionService.promote_to_champion()`/
  `ModelVersionRecord`/`WarmStandbyCache` already existed, but
  `_get_model_obj()`/`_install_model()` only recognised `tft_forecaster`/
  `ma_forecaster`/`garch`/`tail_risk`/`corr_graph` — none of which go
  through a real train-then-load cycle (TFT is a stub, GARCH/tail-risk/
  corr-graph are self-contained online learners, MA is a stateless
  baseline). The two forecasters that DO have a genuine retrain→validate→
  save cycle (`gbm_forecaster`, `intraday_forecaster`) were never wired
  into promotion at all — `reload_neural_models()` (the Section 21A fix)
  was reconstructing a brand-new `NeuralPredictionService` from scratch on
  every reload, bypassing the version/warm-standby system entirely and
  discarding unrelated models' state each time.

  Fixed: extended `_get_model_obj`/`_install_model` to cover both
  forecasters; added `NeuralPredictionService.reload_forecaster_from_disk()`
  which loads the artifact, checks `is_available()`, and only calls
  `promote_to_champion()` — with a timestamp version and the artifact's own
  recorded walk-forward metrics — when it actually validated; a rejected/
  absent artifact leaves the current champion untouched. `reload_neural_
  models()` now reuses the SAME `NeuralPredictionService` instance instead
  of rebuilding it, so champion/challenger history and the warm-standby
  cache persist across reloads.

  Still not done: no checksum validation, no dataset-hash/code-revision
  stamping on the `ModelVersionRecord`, and `StrategyPromotionService`
  (strategy-level promotion) remains a fully separate registry from
  `ModelVersionRecord` (model-level) — no single registry authority as
  §21I ultimately asks for. Unifying those two would be a larger,
  cross-cutting change deferred for a future session.

  Tests: `tests/test_runtime.py::test_reload_neural_models_reports_status_without_a_saved_artifact`,
  `::test_reload_neural_models_promotes_a_validated_artifact_through_champion_path`
  (trains a real model on genuine AR(1) synthetic edge, saves it, reloads
  it, and asserts it was promoted to `status: "champion"` and is actually
  serving). Full suite: 1476 passed. Deployed live.
- **21K** (acceptance tests for the full lifecycle): some of §21K's listed
  scenarios are independently covered by existing/new tests (open trades
  stay pending — `test_return_forecaster.py`/`test_intraday_forecaster.py`'s
  random-walk rejection covers "no genuine edge produces a truthful skip,
  not a fabricated pass"; §21H's embargo tests cover "training-only
  preprocessing and purged label boundaries prevent leakage"). There is no
  single test file demonstrating the full 9-state lifecycle end-to-end,
  because that lifecycle (§21B) does not exist as one implemented thing to
  test.
- **§21B's actual state machine** (`SESSION_ACTIVE -> ... ->
  NEXT_SESSION_READY`, DB leases, idempotent stages, checkpoints, resumable
  stages) remains unbuilt. This is the one piece of Section 21 that is
  genuinely new architecture, not integration repair — building it without
  a second validated model or real option-chain data to exercise it against
  would mean testing a lot of new machinery against the same "no edge
  found yet" data this deployment already has, which does not by itself
  justify the engineering cost right now. Recorded as the clear next step
  if/when a second model family earns real promotion pressure.

## Section 19 — real bug found and fixed: goal-shortfall was driving automatic risk escalation

Auditing §19's explicit requirement ("Replace the hardcoded annual-profit
target with a user-configurable planning input that **cannot drive
automatic risk escalation**") surfaced a live, active violation, not a
documentation gap: `YEARLY_PROFIT_TARGET` was already env-configurable
(`config.py`), but `GoalGovernor.compute_state()`'s `"increase_research"`
recommendation (emitted whenever the account is behind the yearly target)
was mapped by `MasterOrchestrator._node_goal_governor()` to `"SCALE_UP"`,
which multiplied `position_size_multiplier` up to **1.5x**, which
`trading_agent.py:1068-1071` multiplies directly into real order quantity:
`max(1, int(default_quantity * state.position_size_multiplier))`.

In plain terms: falling behind an arbitrary, user-configurable profit
target was silently widening real order sizes by up to 50% — the exact
"automatic risk escalation" §19 prohibits, and a direct contradiction of
`GoalGovernor`'s own module docstring ("SAFETY: GoalGovernor can NEVER
automatically raise hard risk limits. It can only recommend increasing
research intensity or reducing risk"). "Research intensity" was never
actually about running more backtests/hypotheses
(`scripts/scheduled_research_session.py` already exists for that); it was
silently translated into bigger live bets. Currently reachable only when
`AGENT_DIRECTIONAL_ENABLED=true` (off by default), but this deployment
already runs live paper trading, and this was a landmine that would
activate the moment directional trading is turned on — worth fixing
regardless of today's gating, matching this session's established practice
of not leaving a discovered risk path for later.

**Fix**: `_node_goal_governor()` no longer has any code path that can
raise the multiplier above 1.0 — the `SCALE_UP` branch's multiplier boost
was deleted outright (not just remapped). `REDUCE`/`PRESERVATION` (which
only ever shrink the multiplier) are unaffected — de-escalating size when
behind target and low on drawdown budget is legitimate risk management,
not the kind of escalation §19 prohibits. `"SCALE_UP"` is still reported in
`goal_recommendation` for dashboard transparency (§19: "show why") — it
simply no longer feeds sizing.

Tests: `tests/test_master_orchestrator_goal_governor_node.py` (3 new) —
directly assert `increase_research` can never push the multiplier above
1.0, `reduce_risk` still shrinks it, and `normal` leaves it unchanged. Full
suite: 1479 passed. Deployed live.

## Follow-up sweep for other advisory-signal-with-no-effect gaps

Having found the goal-governor escalation bug by asking "does an advisory
signal quietly affect real risk beyond intended bounds," swept the rest of
`master_orchestrator.py`'s 9 nodes for the same shape of bug (in both
directions — a signal that should matter but doesn't, and one that
shouldn't matter but does):

- `_node_risk_critic` (Node 4) already halts directly and unconditionally
  on drawdown/daily-loss breach — correct, no gap.
- `_node_strategy_selection`'s validated-strategy quantity is correctly
  NOT rescaled by `position_size_multiplier` in `_node_execution_plan` —
  a real, risk-approved strategy quantity should not be arbitrarily
  resized by an advisory goal signal. Correct as-is.
- **Real gap found**: `_node_risk_critic` computes `risk_score = max(drawdown,
  daily_loss_pct)` specifically "so Node 7 can weight it" per its own
  comment — but nothing downstream ever read `state.risk_score`. Not a
  safety hole (the hard breach halts happen inline, in the same node,
  regardless), but a real gap between a documented design intent and the
  actual code: the fusion score gave zero extra weight to drawdown/daily-
  loss proximity below the hard breach threshold. Fixed by folding
  `risk_score * 0.3` into `_node_consensus_fusion`'s existing penalty term
  (same pattern as its uncertainty/tail-risk/correlation penalties) — a
  rising risk_score now measurably reduces `fusion_score`, and can tip an
  otherwise-proceeding signal into HOLD before any hard limit is breached.

Tests: `tests/test_master_orchestrator_consensus_fusion_node.py` (2 new) —
confirm a nonzero risk_score reduces fusion_score by the exact expected
amount, and that a high (sub-breach) risk_score can tip a borderline signal
below `FUSION_PROCEED_THRESHOLD` into a halt.

**Also found and fixed while exercising the new report live**: a real
UTC/IST timezone bug in `daily_learning_report()` itself (Section 21J,
this session's own earlier work) — `model_runs.run_at` is persisted as a
UTC timestamp, but the "today's runs" filter compared its raw date
substring against an IST `session_date`, which is wrong for the ~5.5
hours/day where IST's calendar date has already rolled to "tomorrow" while
UTC's has not (IST = UTC+5:30). No *scheduled* job can ever straddle that
window in practice (earliest 08:55 IST = 03:25 UTC, latest 23:25 IST =
17:55 UTC) — this shipped unnoticed for exactly that reason, and was only
caught because this session happened to still be running at 02:44 IST.
Fixed to use `to_ist()` like the rest of the codebase, not a string slice.
Test: `tests/test_runtime.py::test_daily_learning_report_matches_todays_model_runs_across_the_utc_ist_boundary`.

Full suite: 1482 passed. Deployed live.

## Section 19 — dashboard/frontend truthfulness (2026-09-16)

Dispatched a read-only audit (Explore agent) against the checklist in the
implementation prompt's Section 19 — contract drift, distinguishing
targets/simulations/theoretical prices/forward-paper/live results, showing
block/rejection reasons, never presenting a target as expected income, data
provenance/staleness, and hardcoded/fake data. It cross-referenced every
view's API calls against the real `app.py` routes and DB schemas rather
than reading the frontend in isolation. Findings, all verified and fixed:

**1. Strategy filter chips never matched anything, since inception.**
`Signals.tsx`'s hardcoded chip list (`ShortStraddleStrategy`,
`IronCondorStrategy`, `CoveredCallStrategy`, ...) shares **zero** overlap
with the real 19-strategy catalog (`iron_condor`, `long_straddle`, ...,
confirmed live via `GET /strategies/catalog`) — different naming
convention entirely (PascalCase class names vs. the catalog's snake_case).
Every strategy-filtered scan has silently returned zero candidates since
this UI was built. Fixed: chips are now sourced live from
`getStrategyCatalog()`.

**2. A real production-state contamination bug, same class as a prior
incident.** `shadow_run()` — a "Shadow Run" button meant to preview a
what-if scan — actually submitted real PAPER orders through the exact same
shared `self.portfolio` ledger real forward-PAPER trading uses, over a
fixed historical demo window (2026-01-01 by default, sometimes with
synthetic bars) that has nothing to do with "today." `/portfolio/positions`
reads that identical live object. Clicking "Shadow Run" on the live
dashboard would have silently injected backdated demo fills into the real
paper track record — the same failure shape as this codebase's own prior
`shadow_run` feature-store contamination incident, just in the portfolio
ledger instead. Fixed: `shadow_run()` now executes against a fresh, isolated
`PortfolioLedger` scratch instance; the response carries `is_shadow: true`
and the actual `shadow_window` (start/days/used_live_history) so the
frontend can honestly label results and never conflate them with genuine
paper P&L. Frontend: renamed the button, changed its icon away from the
passive-implying "Eye", added a pre-run confirmation explaining what it
actually does, and a result banner showing the real demo window and
fill/reject counts. Test:
`tests/test_runtime.py::test_shadow_run_never_mutates_the_real_portfolio`
(asserts the real portfolio's cash/trades/positions are byte-for-byte
unchanged after a shadow run that did real work).

**3. OMS queue and risk-events table read the wrong column names.**
`Execution.tsx`'s OMS panel read `e.status`/`e.ts`; the real `oms_events`
columns are `event_type`/`occurred_at` — every row fell through to a blank
status and blank time regardless of what actually happened. `Risk.tsx`'s
"Risk Rejections" table read `timestamp` instead of `occurred_at` (same
bug) and wasn't actually filtered to rejections — `risk_events` stores
`event_type="order_evaluated"` for both approved AND rejected orders, so a
table titled "Risk Rejections" could list approved orders. Fixed both
column names; added an `approved !== true` client-side filter; added a
`rejection_reason` display to the OMS panel (was captured by the backend,
never shown) and an event-type column to the risk table.

**4. Price provenance was collapsed to a meaningless binary.** The backend
already computes a 4-tier price source per position via
`PriceResolutionService` (`live` → `model` [Black-Scholes theoretical,
never a real quote] → `sticky` [last known live price] → `entry` [no mark
yet]) and a separate `price_is_stale` flag, but `LivePosition`'s TS type
only declared `live: boolean` and the UI showed "Live"/"Stale" — a
theoretical mark that was NEVER a real quote displayed identically to a
merely-stale-but-once-real tick. Fixed: typed `price_source`/`price_is_stale`
and render four distinct states ("Live", "Theoretical", "Last known
(stale)", "No mark yet").

**5. Equity curve could not distinguish PAPER from a future LIVE session.**
`GET /db/equity-curve`'s unfiltered query selected `recorded_at, equity,
drawdown` only — no `execution_mode` — so a PAPER and a LIVE session's
snapshots would render as one unbroken, unlabeled line the first time this
deployment ever runs LIVE (it never has yet, so not yet an active bug, but
a real gap). Fixed: the column is now always selected; the dashboard shows
an execution-mode badge on the Equity Curve card, or an explicit "Mixed —
filter required" warning if it ever sees more than one mode's data in one
unfiltered fetch.

**6. A hardcoded capital constant silently diverged from the real number.**
`CommandCenter.tsx` hardcoded `startCapital = 1_000_000` and derived
"Total P&L" as `equity - startCapital`, ignoring `settings.initial_capital`
(configurable) and the portfolio snapshot's own real
`realized_pnl`/`unrealized_pnl` split (used two lines later in the same
file for a different tile). An operator who changed the configured initial
capital would see a silently wrong, authoritative-looking P&L figure.
Fixed to sum the real realized+unrealized fields.

**Checked and found NOT broken**: `Signals.tsx`/`Backtest.tsx` strategy
filters ARE honored server-side (not client-side-only filtering);
`Backtest.tsx` already clearly labels synthetic vs. historical data;
`Signals.tsx` and `CommandCenter.tsx` already show real block/rejection
reason strings for candidates and live-readiness gates; no view phrases
the yearly target as "expected"/"projected" income anywhere (GoalGovernor.tsx
was already audited and fixed for this in an earlier session); no
fabricated/mocked data found beyond legitimate `<input placeholder>`
attributes and one `Math.random()` used only as a React list-key fallback.

Tests: `tests/test_runtime.py` (1 new, above). Frontend: `tsc --noEmit`
clean, `npm run build` succeeds, `npm test` 7/7 passed (pre-existing
suite, unaffected). Backend full suite: 1483 passed.

**Not fixed this pass** (lower severity, cosmetic): two different
components both titled "Command Center" (`Dashboard.tsx` and
`CommandCenter.tsx`) presenting inconsistent feature sets under an
identical on-screen name; `CommandCenter.tsx`'s own connection badge
doesn't consult the same freshness signal `Dashboard.tsx`'s does. Neither
is a truthfulness violation, just an operator-clarity nit.

## Infrastructure note: Docker Desktop stopped and was restarted (2026-09-16)

Discovered mid-session while verifying the frontend against the live
backend: Docker Desktop's engine was not running (`docker ps` failed to
reach the daemon; the `docker-desktop` WSL distro showed `Stopped`) — an
OS/environment-level event, not something in this codebase. All containers
carry `restart: unless-stopped`, so relaunching Docker Desktop brought
`trading-api`, `trading-scheduler`, and the rest of the stack back up
automatically within ~35 seconds, healthy. No code or config change
resulted from this; noted here only because it explains a live-verification
gap earlier in this session and as a reminder that this platform's uptime
currently depends on Docker Desktop being running on a single Windows
machine — a real operational fragility (Section 18's "reliable forward-paper
operations") worth a future session's attention (e.g. a watchdog/alert if
the containers go down, or moving off Docker Desktop to a service that
survives a host reboot unattended), not something fixable in code alone.
(It happened again during this same session, a second time, ~2 hours
later — see the entry below.)

## Section 19 follow-up: two more low-severity frontend fixes

- `Dashboard.tsx`'s own `PageHeader` said "Command Center" (copy-paste
  artifact — it already used the `LayoutDashboard` icon and "Dashboard" is
  its own sidebar nav label; `CommandCenter.tsx`, a different view, is the
  real "Command Center"). Retitled to "Dashboard".
- `CommandCenter.tsx`'s connection indicator only echoed the raw WebSocket
  status, never consulting the real freshness signal (`useConnectionStatus`)
  `Dashboard.tsx` already uses — so its "Runtime health" card couldn't tell
  an operator the tick feed had gone silent while the socket was still
  nominally connected. Replaced with the shared `FreshnessBadge`.

Frontend: `tsc --noEmit` clean, build succeeds, `npm test` 7/7.

## Section 18 — real gap found and fixed: monitoring status could never report staleness

Audited §18's "Track feed health, stale symbols... Separate technical
health from economic performance" against `GET /monitoring/metrics`,
which is the one general-purpose ops/health surface distinct from the
dashboard's own `/ws/dashboard` snapshot.

`TradingRuntime.monitoring_metrics()` called `self.monitor.snapshot(...,
stale_market_data=False)` — hardcoded, unconditionally. `monitoring/
metrics.py`'s own status rule (`elif stale_market_data or rejection_rate >
0.10 or average_latency > 200: status = DEGRADED`) could therefore **never**
report DEGRADED due to staleness, no matter how stale the feed actually
was — a real "track feed health" gap, distinct from and not affecting the
live order path's own staleness protection (`SimulatedBrokerClient`'s
tick-age rejection, section 6/8 of this same status doc), which reads live
ticks directly and was never touched by this bug.

A real, already-computed freshness signal already exists —
`live_feed_service.py`'s `_freshness_summary()` — and is exactly what the
dashboard's own `useConnectionStatus()`/`FreshnessBadge` already trust (see
the Section 19 audit above, which confirmed that specific signal genuinely
wired, not decorative). `monitoring_metrics()` just never read it. Fixed:
it now derives `stale_market_data` from that same `live_feed_snapshot()`
call, so the two surfaces cannot disagree; any lookup failure fails open to
`False` (an observability signal, not a safety gate) via `note_swallowed`.

Also checked while here: `RiskSupervisorAgent.decide()`'s own
`stale_market_data` halt path (`agents.py`) is reachable only via
`POST /supervisor/decision` — an external-caller-triggered diagnostic
endpoint, never invoked automatically from the live trading loop. Not a
live safety gap (nothing currently depends on it firing automatically),
but noted as inert/standalone infrastructure, matching this session's
recurring "built but never wired" pattern (§21A's meta-labeling pipeline,
§21I's forecaster promotion) — left as-is; wiring it in would be a
judgment call about whether it should become a live gate, not a bug fix.

Tests: `tests/test_runtime.py::test_monitoring_metrics_reflects_real_feed_staleness`
(3 cases: stale→DEGRADED, fresh→HEALTHY-eligible, lookup failure fails
open). Full suite: 1484 passed. Deployed live.

## Infrastructure note: Docker Desktop stopped a second time (2026-09-16)

Same signature as the earlier entry — `docker ps` failed, WSL's
`docker-desktop` distro showed `Stopped` — approximately two hours after
the first occurrence in this same session, unprompted by anything in this
codebase. Restarted again; all containers recovered automatically within
~10s (`restart: unless-stopped`). Two occurrences in one working session
raises this from "noted" to a genuine pattern worth investigating — check
Windows power/sleep settings, Docker Desktop's own update/crash logs, or
consider a supervising watchdog, before assuming it won't recur a third
time unattended.

## Section 11 — wired an already-built, never-called module to real live data

Per the user's explicit direction to use the live/collected tick and
option-chain data for sections 9-17: `trading_platform/strategies/
svi_surface.py`'s SVI/SABR vol-surface fitter, `VolSurface`, and
`assess_strikes()` rich/cheap classifier (REDESIGN_PROMPT.md §4.4a) were
built and unit-tested (`tests/test_svi_surface.py`, fixed from a
previously-broken state per an earlier session) but had **zero production
callers** anywhere in the codebase — confirmed by grep. Unlike the IV-rank
signal (which needed many *days* of history), an SVI smile fits
cross-sectionally across *strikes* within a single day's snapshot, so it
has been fittable from `options_chain_collector`'s real captured data
(running since 2026-07-29) since the very first successful capture — it
just never had anything wiring it in.

Added `OptionsChainCollector.latest_smile()` (scans the real chain-history
CSV backward from the most recent date for one with enough distinct
strikes, skipping a rate-limited/sparse day rather than fitting it anyway)
and `research/vol_surface_diagnostics.py` (fits SVI via the existing
`fit_svi`, classifies each strike rich/cheap via the existing
`assess_strikes`), exposed as `GET /derivatives/vol-surface-diagnostics/
{underlying}`. Deliberately research/diagnostic only — does not feed any
order path or change position sizing; `StrikeAssessment.recommended_action`
is relabeled informational, not an instruction, since wiring rich/cheap
deviations into actual strike selection would be a real strategy change
needing its own backtest validation, which 1-2 months of chain history
cannot honestly support yet.

**A real, worth-recording finding surfaced while wiring it up**: fitted
against this deployment's actual real August-September 2026 chain history,
the SVI fit's own repricing accuracy was measurably poor — a well-behaved
synthetic reference smile (matching `test_svi_surface.py`'s own validated
fixture) reprices to ~0.05 mean-abs vol points, while the real captured
NIFTY/BANKNIFTY/FINNIFTY smiles reprice to 0.53-0.71 — over 10x worse, a
genuine degradation even though "0.7 vol points" sounds small in
isolation, given the real smiles' own directly-observed skew spans only
~2.5 vol points end to end. Root cause: `options_chain_collector.capture()`
only takes strikes ±5 either side of spot, which for NIFTY/BANKNIFTY covers
under 2.3% log-moneyness — too narrow to identify SVI's 5 free parameters
independently (many distinct parameter combinations achieve near-identical
squared error). An initial attempt to detect this by checking whether the
fitted `b` parameter sat near its optimizer-bound floor turned out to be a
dead end: that floor is common to `test_svi_surface.py`'s own validated
*wide* reference fit too, so it isn't a reliable narrow-vs-wide
discriminator — just a property of this smile shape/optimizer landscape.
Replaced with a direct, principled check instead: does the fitted curve
actually reprice the market IVs it was fitted to (the same check
`test_svi_surface.py`'s own `test_fit_reproduces_the_input_smile` uses,
just with a tighter bar appropriate to this deployment's much denser real
strike spacing). The diagnostic now honestly reports `low_confidence_fit:
true` with the specific reason whenever either check fails, rather than
presenting an unidentified curve as a reliable smile — confirmed this
correctly triggers for all three real underlyings today, and correctly
does *not* trigger for a well-identified wide synthetic reference.
Widening `strikes_each_side` in the live collector would address the root
cause but increases Angel One API call volume against a rate limit this
system has already had to build a circuit breaker around
(`_run_chain_capture_tick`) — left as a deliberate, disclosed trade-off for
a future explicit decision, not changed silently from a diagnostics
feature.

Tests: `tests/test_options_chain_collector.py::LatestSmileTests` (5 new),
`tests/test_vol_surface_diagnostics.py` (5 new, including a direct
wide-vs-narrow comparison against the exact synthetic-smile formula
`test_svi_surface.py` already established). Full suite: 1494 passed.

## Section 18 — dispatched audit, three real findings fixed, rest documented

Dispatched a read-only audit (Explore agent) against Section 18's full
checklist (unresolved orders, blocked exits, unexpected exposure, session
baseline integrity, cost drift, fault-injection coverage for 6 named
failure modes, account-data protection across REST/WS/logs) — deliberately
scoped to exclude the `stale_market_data` finding already fixed earlier
this session. Findings and what was done with each:

1. **Unresolved orders — real gap, fixed.** Detection was already real and
   automatic (`scheduler.py`'s `_track_order_until_terminal` records
   `fill_unresolved`/`fill_price_missing` OMS events), surfaced on-demand
   via `GET /execution/unresolved-orders`, but nothing periodic ever read
   it — an order could sit unresolved indefinitely without moving
   `/monitoring/metrics` off `HEALTHY`. Fixed: `monitoring_metrics()` now
   passes the real count into `monitor.snapshot()`, and
   `monitoring/metrics.py`'s `OperationalSnapshot` gained an
   `unresolved_orders` field feeding the same `DEGRADED` rule staleness
   already does. Test:
   `tests/test_runtime.py::test_monitoring_metrics_reflects_real_unresolved_orders`.
2. **Blocked exits — compliant**, with a caveat: `ExitManager._monitor_loop`
   really does run continuously and escalates to `logger.critical` after
   `MAX_EXIT_RETRIES`, which reaches the runtime-monitor digest via the
   existing WARNING+ log handler — but there is no dedicated counter/
   endpoint the way `unresolved_orders` now has. Not fixed this pass;
   recorded as a smaller follow-on for symmetry with the fix above.
3. **Unexpected exposure (broker reconciliation) — compliant for LIVE,
   architecturally a no-op in PAPER** ("the paper broker IS the ledger" —
   a reasonable design choice, not a bug, since this deployment has never
   run LIVE).
4. **Session baseline integrity — confirmed intact**, no regression.
5. **Cost drift — real gap, not fixed.** `execution/tca.py` only computes
   point-in-time aggregates; nothing compares real transaction costs
   against the costs *modeled* in backtesting/validation over time, and
   `export_for_backtest_calibration()` (meant to feed that comparison back
   in) has zero callers anywhere — dead code. Left for a future session:
   building genuine trend/alerting here is a real feature, not a bounded
   fix, and this deployment's short real trading history wouldn't yet
   support a meaningful drift baseline.
6. **Fault-injection coverage — mixed.** Restart/crash recovery, feed gaps,
   and inference-unavailable all have real tests. Investigated the
   "duplicate broker callback" gap specifically since a double-booked fill
   would corrupt real portfolio state: traced the actual delivery path
   (`_track_order_until_terminal`'s polling loop, single tracking task per
   acknowledged order, immediate `return` after the first terminal
   detection, idempotency-key dedup at submission) and confirmed by direct
   grep that **no separate order-update webhook exists in this codebase at
   all** — the polling loop *is* the only delivery path, and it structurally
   cannot double-fire for one order. Added
   `tests/test_audit_fixes.py::LiveFillTrackingTests::test_duplicate_complete_status_does_not_double_book_the_fill`
   (scripts the fake broker to report "complete" three times in a row —
   the closest reproduction available at this interface — and asserts
   exactly one fill reaches the ledger) to lock in that guarantee against
   a future refactor, rather than writing a test for a webhook mechanism
   that doesn't exist. Missing options legs and general DB-write-failure
   fault injection remain real, unaddressed gaps — left for a future
   session (both need real scoping, not a quick patch).
7. **Account-data protection — real gap, fixed.** WebSocket/REST auth
   gating is real and precise, but `strategies/strategy_engine.py`'s
   `reset_peak()` interpolated the real rupee equity figure into an
   INFO-level log line — unauth-gated, unlike every other surface exposing
   the same figure. Fixed: logs that a reset happened at INFO, the figure
   itself at DEBUG only.

Tests: 2 new (above) plus the duplicate-callback regression. Full suite:
1496 passed. Deployed live.

## Section 18 — remaining documented gaps, closed out

Continuing from the previous round's punch list (blocked-exits counter,
cost-drift tracking, missing-legs and DB-write-failure fault injection):

**Blocked exits — fixed.** Added `ExitManager.blocked_exit_plans()`
(positions whose exit has failed to confirm `MAX_EXIT_RETRIES`+ times —
the same condition the monitor loop already `logger.critical`s as "may be
STUCK OPEN"), exposed via `TradingRuntime.blocked_exit_plans()`,
`GET /execution/blocked-exits`, and wired into `monitoring_metrics()`'s
`DEGRADED` rule alongside `unresolved_orders` (both `OperationalSnapshot`
and `monitor.snapshot()` gained matching parameters). Tests:
`tests/test_audit_fixes.py::ExitPlanLifecycleTests::test_blocked_exit_plans_reports_a_stuck_position`,
`tests/test_runtime.py::test_monitoring_metrics_reflects_real_blocked_exits`.

**DB-write-failure fault injection — real gap found and fixed, beyond
what was previously scoped.** Investigating this surfaced something the
prior audit agent's one-narrow-call-site finding didn't catch:
`_on_fill`'s DB persistence (`save_trade`/`save_snapshot`/`save_positions`)
used bare `asyncio.ensure_future(asyncio.to_thread(...))` — fire-and-forget
with nothing ever awaiting the resulting `Task` or checking
`task.exception()`. The fill itself was never at risk (the ledger updates
in-memory before `_on_fill` even runs), but a genuine DB failure during
that persistence step would vanish completely: not logged, not counted via
`note_swallowed`, invisible in `/health`'s swallowed-error counter — a real
"database failure" blind spot. Added `TradingRuntime._persist_async()`
(same non-blocking shape, but the failure is now observed) and switched
all three call sites to it. Test:
`tests/test_runtime.py::test_persist_async_reports_a_db_failure_instead_of_losing_it_silently`.

**Missing options legs — fixed.** `MultiLegOrderManager` already has real
leg-timeout/rollback logic (`submit()`'s `asyncio.wait_for` + `_rollback()`)
but every existing multi-leg test stubbed `submit()` wholesale, never
exercising it. New `tests/test_multi_leg_manager.py` drives the real
manager directly with a fake `enqueue_fn` that intentionally never
confirms one leg's fill — proving the manager times out, marks that leg
`FAILED`, and reverses the already-filled leg with an opposite-side
`EMERGENCY_EXIT` intent, rather than leaving a naked half-open spread.

**Cost drift — investigated further; a real, separate bug found and fixed
in the process, but the drift-tracking feature itself is not buildable
yet, precisely (not just "not enough data").** Building the comparison
required first establishing what "the real cost" even means in this
codebase, which led to `execution/tca.py::TransactionCostAnalyzer.
compute_summary()`: its avg/median/percentile/per-symbol headline "cost"
stats aggregated `TCAResult.total_cost_bps` — but `analyze_fill()`'s own
comment says that field "is a DIAGNOSTIC decomposition only ... and must
never be used as 'the cost'", because its three components use different
sign conventions (`impact_bps` is positive-means-*favourable*, the other
two are positive-means-cost). A large favourable price move after a fill
inflated the reported cost rather than netting against it — one
constructed scenario (real shortfall ≈100bps) reported ≈4089bps, a >40x
overstatement. `total_cost_inr` a few lines below in the same function
already correctly used `abs(implementation_shortfall_bps)`;
`compute_summary()` just never matched it. Fixed to use the same
convention throughout. No test file existed for this module at all before
now — added `tests/test_tca.py` (5 new tests) covering both the fixed
aggregation and the underlying per-fill computation.

Separately flagged, not fixed (uncertain, needs fresh verification):
`analyze_fill()`'s `impact_bps` formula doesn't branch on `side` the way
`implementation_shortfall_bps`/`spread_bps` do, and the function's own
comment describes the "positive = favourable" convention only for the BUY
case — a SELL where price *drops* after the fill (favourable — sold high
before a decline) may compute a *negative* impact_bps under the same
formula, the opposite sign from the documented BUY convention. Not
confirmed as a bug (didn't want to rush a fix to a subtle sign-convention
question without being certain), but worth a future session's careful,
side-by-side verification.

**Why cost-drift-vs-model tracking specifically isn't buildable yet**:
not merely "too little data" (the framing used in the previous round) —
this deployment's `execution_mode` has only ever been `PAPER`
(`fills_analyzed: 0` right now, confirmed live), and PAPER fills are
generated *by* `SimulatedBrokerClient._apply_slippage()`'s own modeled
spread/impact assumptions (`spread_bps=4.0` default). Comparing those
fills against the same model they were generated from would be
tautological, not a genuine drift signal — this needs real LIVE broker
fills to mean anything at all, which requires LIVE trading to be armed, a
decision already explicitly deferred by the user earlier in this session.
Recorded here so a future session builds this against the right data
source once it exists, rather than building a plausible-looking feature
with nothing real to measure.

Tests this round: 4 new files/additions (`test_multi_leg_manager.py`,
`test_tca.py`, plus additions to `test_runtime.py` and
`test_audit_fixes.py`). Full suite: 1506 passed. Deployed live.

## Section 21B — a real, bounded slice of the durable state machine (built)

The full spec (`SESSION_ACTIVE -> SESSION_CLOSED -> RECONCILED ->
DATASET_READY -> EVALUATED -> TRAINED_OR_SKIPPED -> CANDIDATE_VALIDATED ->
SHADOW_OR_PAPER_ELIGIBLE -> NEXT_SESSION_READY`, DB leases, idempotent
stages, checkpoints) was previously deferred as "genuinely new
architecture." Rather than build the whole exchange-session lifecycle at
once — most of which (SESSION_ACTIVE/CLOSED/RECONCILED) belongs to other
jobs (`daily_pnl_report`, `eod_square_off`) that already run and already
have their own re-fire protection (the grace-window fix) — this pass built
the concrete, high-value slice the spec explicitly motivates: durable,
lease-protected tracking for the two jobs that actually run real training
(`model_retrain`, `intraday_retrain`), closing a genuine, verifiable gap.

**The gap**: `daily_scheduler.py`'s `main()` loop tracks "already ran
today" via an in-memory `last_run_date` dict. That dict resets to empty on
every process restart — so a scheduler crash/restart mid-day could
re-trigger the same day's `model_retrain` a second time, directly
contradicting the spec's "duplicate scheduler processes/restarts cannot
double-count." Low probability, real consequence (wasted 30-minute compute
budget, or worse, two concurrent trainers racing to save the same model
artifact).

**Fix**: a new `learning_runs` table (`UNIQUE (job_name, session_date)` is
the actual idempotency mechanism) plus `TradingDatabase.claim_learning_run()`
/ `update_learning_run()` / `recent_learning_runs()`. Claiming a run is one
INSERT that either succeeds (this process owns today's run) or hits the
constraint — the existing row's `state` and `lease_expires_at` then decide
whether to refuse (already terminal today, or another process holds a
still-valid lease) or take over (a crashed process's stale lease — the
recovery path, not a duplicate-run risk, since the underlying training
mutation is a pure refit from re-fetched data, safe to redo from scratch).
`model_retrain`/`intraday_retrain` now claim before doing any work and
record real state transitions at each existing decision point:
`MISSING_DATA`, `DATASET_READY`, `EVALUATED`, `TRAINED_OR_SKIPPED`,
`VALIDATION_REJECTED`, `CANDIDATE_VALIDATED` (saved but live-reload
unconfirmed), `SHADOW_OR_PAPER_ELIGIBLE` (saved AND confirmed served —
reusing the Section 21I `_reload_neural_models()` return value), or
`OPERATIONAL_FAILURE` on an unexpected exception. Lease duration (30 min)
matches the existing Section 21B time-budget fix
(`_JOB_TIME_BUDGET_SECONDS`), so a legitimately-still-running job's lease
is never mistakenly seen as expired.

Exposed via `GET /learning/runs` (multi-day history) and folded into
`GET /learning/daily-report`'s existing `learning_runs` section (today
only). `daily_scheduler.py`'s own in-memory `last_run_date` dict is left
in place unchanged — it still governs the *other* jobs (`instrument_refresh`,
`eod_square_off`, etc.) that don't have this durable protection, and
keeping it doesn't conflict with the new mechanism (the DB claim would
refuse a re-run regardless of what the in-memory dict does).

**Explicitly not built**: the full `SESSION_ACTIVE`/`SESSION_CLOSED`/
`RECONCILED` exchange-session lifecycle, per-stage checkpointing within a
single job (these two jobs' own training mutation is one atomic unit, not
several resumable stages), and applying this pattern to jobs beyond the
two training ones. Recorded as the clear next increment if a future
session finds a concrete need (e.g., a third training job, or genuine
partial-mutation resumability becoming necessary).

Tests: `tests/test_learning_runs.py` (8 new — claim/refuse/lease-expiry-
takeover/multi-job-multi-day isolation, directly against a throwaway
SQLite-backed `TradingDatabase`), `tests/test_daily_scheduler.py::LearningRunWiringTests`
(4 new — refused claim skips all real work; a real tiny-dataset run
through the actual `GradientBoostedReturnForecaster` correctly records
`DATASET_READY`/`EVALUATED`/`TRAINED_OR_SKIPPED`), `tests/test_runtime.py`
(2 new — report/endpoint surface real claimed runs). Full suite: 1520
passed. Deployed live (both `trading-api` and `trading-scheduler`, since
both load `persistence.py` and the scheduler specifically needs the
`daily_scheduler.py` changes).
