# Edge expansion — NIFTY + BANKNIFTY short-vol added (2026-11-04)

**Paper only.** `LIVE_TRADING_ENABLED` stays false. This widens the approved
short-vol edge from FINNIFTY-only to the three NSE index underlyings that meet
the same validation standard FINNIFTY was approved on, so more index-options
trade *when their premium is rich* — without loosening a single gate.

## What changed
- `APPROVED_EDGES`: `short_vol:FINNIFTY:4.0`
  → `short_vol:FINNIFTY:4.0,short_vol:NIFTY:2.0,short_vol:BANKNIFTY:1.0`
- `SHORTVOL_AUTO_UNDERLYINGS`: `FINNIFTY` → `FINNIFTY,NIFTY,BANKNIFTY`
- `config.py` default updated to match; `.env` updated for the running stack
  (backup `.env.bak-edge-expansion-20261104`).

Each underlying keeps its OWN walk-forward-chosen regime theta, so each still
**stands aside when its trailing-60d premium is below theta**, and the strategy's
own current-VRP-richness check and the tail cap still apply. This enables more
opportunities; it does not force any trade.

## Evidence (authoritative re-validation, 2026-11-04)
Re-ran `scripts/validate_index_strategies.py` — the 54-trial jointly-deflated
per-index sweep through the full DSR/PBO/CPCV/MC-DD/cost gates on each index's
own ATM IV30 (audit_results/2026-09-19/index_strategy_fit.md):

| index | best structure | Sharpe | CAGR | win% | single-sweep DSR | walk-forward OOS Sharpe (premium_regime_filter.md) |
|---|---|---|---|---|---|---|
| BANKNIFTY | condor hold-5 | **2.09** | 14.55% | 89% | 0.40 (bar 0.5) | 0.76 (theta ≥1) |
| FINNIFTY *(already approved)* | condor hold-5 | 1.35 | 10.17% | 85% | 0.00 | 0.47 (theta ≥4) |
| NIFTY | condor hold-5 | 1.08 | 5.38% | 84% | 0.00 | 0.78 (theta ≥2) |
| MIDCPNIFTY | put-spread | **−2.64** | −2.38% | 0% | 0.00 | — |

**Key fact:** *every* index (FINNIFTY included) FAILS the strict single-sweep
DSR. FINNIFTY is approved anyway, deliberately, on its pre-committed walk-forward
evidence (AUDIT_FIX_2026-10-13). By that **same** standard, BANKNIFTY (higher raw
Sharpe, higher win rate, DSR closest to the bar, OOS 0.76) and NIFTY (OOS 0.78)
have evidence **at least as strong** as FINNIFTY (OOS 0.47). Including them is the
*consistent* application of the existing approval rule, not a weakening of it.

## What was deliberately NOT changed — and why (no edge = no trade)
- **MIDCPNIFTY options**: excluded — negative backtest (Sharpe −2.64, loses money).
- **SENSEX / BANKEX options**: not added — they trade on BSE (BFO); the premium
  monitor, IV history and validation are NSE-only. Untestable without BSE data
  plumbing; expected payoff low (their NSE cousins barely qualify).
- **Stocks, futures, MCX, directional indices, gap, index-basis, FX**: all tested,
  all failed realistic-cost / full-grid validation (CANDIDATE_EDGE_SWEEP_2026-10-13,
  CANDIDATE_INDEX_BASIS_2026-11-04). Forcing them to trade loses money; not done.

## Honest caveats
- All three index edges fail the strict DSR; this rests on the walk-forward, same
  as FINNIFTY. The short-vol premium had decayed through 2026 and these OOS
  Sharpes are modest (0.47–0.78). This is a reasonable **paper** stance to gather
  more live-paper evidence across indices, not proof of live profit.
- IV history used was through 2026-09-18; a fresh `build_index_iv_history.py` +
  re-run is the periodic check for decay/re-emergence.
