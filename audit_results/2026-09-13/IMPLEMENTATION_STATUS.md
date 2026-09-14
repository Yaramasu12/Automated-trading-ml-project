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
