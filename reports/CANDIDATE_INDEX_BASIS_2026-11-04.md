# Candidate: index cash-futures basis mean-reversion — 2026-11-04

Completing the edge-discovery data coverage (fixing the futures fetch that was
broken during the 2026-10-13 sweep) surfaced one new screen-level candidate:
**NIFTY/BANKNIFTY cash-futures basis mean-reversion**. This report runs it
through the same realistic-execution go/no-go that killed the gap signal.

## The screen (research_index_basis.py) — looked promising
At a flat 0.05–0.10% round-trip cost the screen passed: NIFTY IC(basis vs fwd
basis change) = −0.61, Sharpe ≈ 4 at 0.10% cost; BANKNIFTY similar. Trade: when
the near-month basis (future/spot − 1) is rich, short the future / long the
cash, and capture the convergence over a few days.

## Why the screen is not the verdict
1. **Flat cost is an idealization.** The P&L is a *relative-value basis change*
   — it requires trading TWO legs (future + cash), each in and out = four fills.
2. **The cash leg is the killer.** A multi-day hold makes the cash leg a
   DELIVERY trade, traded via a liquid index ETF (e.g. NIFTYBEES): 0.1% sell-side
   STT + stamp + ETF half-spread + tracking error.
3. **Half the signal is not executable.** Retail cannot hold a SHORT cash/ETF
   position overnight in India, so the "basis cheap → long future / short cash"
   side is dropped; only the "basis rich → short future / long cash" side trades.
4. **The convergence is partly mechanical** — basis *must* decay to ~0 by
   expiry (cost of carry), so a negative IC is expected and does not by itself
   imply a profit opportunity net of costs.

## Realistic two-leg cost (trading_platform/backtesting/basis_execution_cost.py)
**Round-trip = 0.310% of notional** = futures leg 0.051% + cash/ETF leg 0.258%.
The cash leg dominates (delivery STT + ETF spread + tracking error).

## The gate (scripts/run_index_basis_gates_realistic.py) — FAIL
Executable (rich-basis) side only, a 9-config HORIZON × entry-threshold sweep,
each trade charged the 0.310% realistic cost:

| HORIZON | threshold | trades | gross/trade | net/trade | Sharpe |
|---|---|---|---|---|---|
| 2 | 60% | 124 | +0.101% | **−0.209%** | −14.6 |
| 3 | 67% | 83 | +0.175% | **−0.135%** | −7.3 |
| 5 | 75% | 48 | +0.261% | **−0.048%** | −1.8 (best) |

Every config is net-negative. Even concentrating on the fewest, richest-basis
trades (widest threshold, longest horizon) lifts gross to +0.261% — still below
the 0.310% cost.

| gate | result |
|---|---|
| deflated_sharpe | **FAIL** — DSR 0.0000, SR_hat −1.78 (n_trials=9) |
| pbo | pass 0.0000 (losing variants just aren't overfit to each other) |
| cpcv | **FAIL** — OOS Sharpe −2.65 |
| monte_carlo_dd | pass (positions tiny → small DD) |
| cost_model | **FAIL** — net/gross −0.185 (net is negative) |
| **all gates** | **FAIL** |

## Decision: NOT promoted
The basis mean-reversion is a statistically real pattern on the (synthetic
continuous) series, but it is **too small to survive a realistic two-leg
execution cost** — the exact "real-but-too-small after costs" wall the gap
signal hit (reports/CANDIDATE_EDGE_SWEEP_2026-10-13.md). The cash-leg delivery
STT + ETF spread alone (~0.26%) swamp the ~0.1–0.26% gross basis edge, and the
cheap-basis half of the signal is not even executable for retail overnight.

**FINNIFTY short-vol remains the only validated, cost-surviving edge.** This is
the disciplined framework working as intended — a lead, honestly validated, then
declined on the economics rather than traded on a screen.
