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
| 1 | opens_position=False bypasses kill switch on a flat account | Critical | pending | |
| 2 | Deployed API auth disabled, published on all interfaces | High | pending | |
| 3 | Outdated STT rates; short-vol charges use net credit not gross per-leg turnover | High | pending | |
| 4 | Backtest metrics drop lot multiplier (₹500 ledger vs ₹10 metric) | High | pending | |
| 5 | Generic backtester: lookahead, option/underlying price mixing, holding-period bugs | High | pending | |
| 6 | LIVE emergency reduction can be blocked by overall readiness during kill switch | High | pending | |
| 7 | Session-loss baseline uses cost-basis equity, resets on restart | High | pending | |
| 8 | Exchange holiday calendar wrong (Holi, Sept 14, Diwali, others) | High | pending | |
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
