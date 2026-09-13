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
| 2 | Deployed API auth disabled, published on all interfaces | High | pending | |
| 3 | Outdated STT rates; short-vol charges use net credit not gross per-leg turnover | High | **FIXED** | Verified current rates via NSE Circular Ref. No. 02/2026 (Budget 2026 hike, effective 2026-04-01): futures STT sell-side 0.02%→0.05%, options STT sell-premium 0.10%→0.15% (old code had 0.0125%/0.0625% — wrong for both eras). `ChargesModel.estimate()` gained a date-effective `as_of` param (both live call sites now pass the real fill date) so a future replay spanning the hike uses the rate that actually applied. Separately, `short_vol_backtest.py::_charges()` was handed a condor's NET credit/debit and taxed the whole thing as one sell-side turnover; new `_leg_turnovers()` prices each leg individually (reusing the existing Black-Scholes/smile-IV pricer) and tracks entry+exit gross sell/buy turnover per leg side, which also let the `is_long_vol` branch-specific charge-argument-swapping be deleted (the same formula is now correct for every structure). Tests: `tests/test_charges_model.py` (7 cases), `tests/test_short_vol_backtest_charges.py` (5 cases). Full suite: 1439 passed. |
| 4 | Backtest metrics drop lot multiplier (₹500 ledger vs ₹10 metric) | High | **FIXED** | `Trade` gained a `lot_size: int = 1` field (default preserves every existing call site); `PortfolioLedger.apply_fill` (the only construction site) now always passes the real `instrument.lot_size`. `round_trip_pnls()` multiplies by the closing trade's `lot_size` (mirroring how the ledger's own realized_pnl always reads it fresh at the closing fill); charges are not multiplied (already in rupees). Also persisted to the `trades` table (additive migration) for reconciliation — **known limitation**: pre-migration historical rows backfill to 1, wrong for any historical derivative trade; flagged in the migration comment, not silently trusted. Verified against the audit's own `probes.py`: ledger PnL 500 now matches `round_trip_pnls` output exactly (was 10). Tests: `tests/test_backtest_metrics.py` (5 cases). Full suite: 1415 passed. |
| 5 | Generic backtester: lookahead, option/underlying price mixing, holding-period bugs | High | pending | |
| 6 | LIVE emergency reduction can be blocked by overall readiness during kill switch | High | **FIXED** | Fixed alongside #1 (same function): a verified reduction (per the finding-#1 derivation, not the caller's claim) may bypass ONLY the `kill_switch` readiness gate; every other gate (execution mode, live flag, confirmation phrase, broker credentials, instrument freshness, feed staleness) still fully applies. Tests: `test_live_reduction_bypasses_only_the_kill_switch_readiness_gate`, `test_live_entry_still_blocked_by_kill_switch_readiness_gate`, `test_forged_reduction_on_flat_symbol_still_blocked_in_live_gate` (tests/test_runtime.py). |
| 7 | Session-loss baseline uses cost-basis equity, resets on restart | High | **FIXED** | New `session_baselines` table (one row per IST trading date) + `TradingRuntime._establish_session_baseline()`: the premarket routine (`force_fresh=True`) always computes a fresh MARKED (not cost-basis) equity via the same price-resolution chain `/portfolio/positions` uses, and persists it; process startup (`force_fresh=False`) restores today's persisted value if one exists (a mid-session restart can no longer silently recompute/reset it), computing fresh only on a genuine cold boot with nothing persisted yet. Tests: `tests/test_session_baseline.py` (5 cases, including the exact restart-preserves-this-morning's-baseline regression). Full suite: 1427 passed. |
| 8 | Exchange holiday calendar wrong (Holi, Sept 14, Diwali, others) | High | **FIXED** | Corrected the 2026 `_HOLIDAYS` set in `agent/market_hours.py`, cross-verified against two independent sources that agree exactly (NSE's own circular PDF timed out on fetch — re-verify against it and the holiday-notice page when reachable, both cited in the module comment). Fixed: Holi (was 25 Mar, actually 3 Mar), Diwali (was "4 Nov Laxmi Puja" — wrong date AND wrong holiday name; correct is "10 Nov Diwali-Balipratipada" — 8 Nov is a Sunday special Muhurat session, not a closure). Added 6 previously-missing holidays: Ram Navami, Mahavir Jayanti, Bakri Id, Muharram, Ganesh Chaturthi (Sept 14 — the single most urgent fix, literally the next day), Dussehra, Guru Nanak Jayanti. 2027 entries left as-is and still marked tentative — NSE has not published that circular yet. Tests: `tests/test_market_holidays_2026.py` (7 cases). Full suite: 1422 passed. **Not yet done**: sections 5/21's broader ask for a versioned, date-effective, expiry-adjustment-aware calendar system — this fix corrects the data, not the architecture. |
| 9 | short_vol validation narrower than deployed config, cost-sensitive | High | pending | |
| 10 | Dashboard strategy filter field mismatch; synthetic 2-point equity curve | Medium | pending | |
| 11 | Trace joins broken (0 joined observations); promotion reports 0 paper days | Medium | pending | |

Sections 12-21 of the implementation prompt (execution realism, attribution,
research backlog, ML/LLM validation, promotion, daily learning pipeline) are
large R&D programs, not single findings — tracked separately below once the
numbered findings above are through triage, per the prompt's own dependency
ordering ("complete mandatory correctness/security/data work before
interpreting strategy improvements").

## Section 21 (daily learning pipeline) — audit and integration status

Not started. Requires sections 1-8 above to be settled first (accounting,
costs, calendar, session baselines) since the learning pipeline consumes
their output.
