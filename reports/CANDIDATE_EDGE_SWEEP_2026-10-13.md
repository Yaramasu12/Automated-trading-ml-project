# Candidate-edge sweep — 2026-10-13

Ran every research/validation candidate through its screen/gates to see if any
edge beyond FINNIFTY short-vol is promotable. **Result: none is.** `APPROVED_EDGES`
stays `short_vol:FINNIFTY:4.0`.

## Gate validators (full DSR/PBO/CPCV/MC-DD/cost)
| candidate | verdict |
|---|---|
| `validate_longvol_structures` (long_straddle, strangle) | **FAIL** — MC-DD 32%/63% (≫15% limit); gross P&L ≤ 0 |
| `validate_dormant_strategies` (equity_momentum, swing_trend, gap_strategy, mean_reversion, breakout, volatility_breakout_options × 10 symbols) | **FAIL, all** — negative/low Sharpe or 20–82% drawdowns; the only "passing" DSRs were 4-config flukes flagged *search too narrow* |

## Research signal screens (OOS AUC / IC vs shuffle-null, cost-aware)
| candidate | verdict |
|---|---|
| classic_momentum (12-1) | no edge (AUC 0.49, p=0.78, net-neg) |
| cross_sectional | no edge (AUC 0.50, loses after cost) |
| sector_rotation | no edge (IC 0.029, p=0.14) |
| turn_of_month | no edge (p≈0.69) |
| lowvol_reversal | no edge (loses badly after cost) |
| commodity_carry | no edge (IC −0.059, p=0.30) |
| commodity_pairs | no edge (p=0.61, fails cost) |
| index_basis | untestable — 0 overlapping futures bars |
| usdinr_overlay | **errored** — `ValueError: 'CDS' is not a valid Exchange` (broken candidate) |
| vol_premium | REAL — but this is the FINNIFTY short-vol edge already deployed |
| intraday_edge / intraday_backtest / vwap_pinning / intraday_commodity_trend | no edge (AUC≈0.50, or real-but-too-small after cost) |
| **gap (gap_calendar_volume + gap_backtest)** | **real signal, but NOT promoted — see below** |

## The gap signal: PASSES the full gate suite — the one real candidate

Built `scripts/run_gap_gates.py` to run the gap open-to-close signal through the
**same gates short_vol had to clear**, over a genuine 24-config parameter sweep
(margin × n_estimators × max_depth × learning_rate) at a conservative 0.20%
round-trip cost. Result (`strategy_id=gap_open_to_close`):

| gate | result |
|---|---|
| deflated_sharpe | **PASS** DSR=1.0000, SR_hat=5.96, SR_0=2.74, n_trials=24 |
| pbo | **PASS** 0.0000 |
| cpcv | **PASS** OOS Sharpe 5.96, AUC 0.5947 |
| monte_carlo_dd | **PASS** 95% DD 5.2% (≤15%) |
| cost_model | **PASS** net/gross 0.6043 (≥0.60 — barely) |
| **all gates** | **PASS** |

This is the first non-short-vol candidate to clear the bar, and it's a real
pass (deflated over 24 configs, PBO 0, OOS-robust). Earlier the gated *factory*
`gap_strategy` failed — but that's a cruder single-config rule; the gated signal
here is the GBM walk-forward with the real feature set.

**Cost sensitivity (best config, 34 trades/day) — the decisive diagnostic:**

| round-trip cost | Sharpe |
|---|---|
| 0.00% | 9.86 |
| 0.20% | 5.96 |
| 0.30% | 4.01 |
| 0.40% | 2.06 |
| 0.50% | 0.11 (breakeven) |
| 0.60% | −1.84 |

So the edge has **real headroom** (breakeven ≈0.50% round-trip) — not a fantasy
that dies at 0.05%. But a realistic open-auction cost of 0.30–0.40% across ~34
NSE names/day consumes much of it. The gates cannot vouch for that: they test
selection bias, OOS robustness and DD, **not** whether ~34 market-on-open fills/
day are achievable at <0.40–0.50% effective cost.

**Status: gate-cleared under flat cost, then KILLED under realistic cost — NOT promoted.**

### Realistic-execution re-gate (the go/no-go) — FAIL

Built `trading_platform/backtesting/execution_cost.py` (India intraday model:
discount brokerage with the Rs-20 floor, STT/stamp/exchange/SEBI/GST, a
liquidity-tiered open-auction half-spread keyed off each name's trailing
turnover, and sqrt market impact) and re-ran the full suite via
`scripts/run_gap_gates_realistic.py`, charging every (symbol, day) trade its own
cost with equal-weight sizing across the day's names.

| gate | result |
|---|---|
| deflated_sharpe | **FAIL** SR_hat = **−0.67** |
| pbo | pass 0.0 (losing variants just aren't overfit to each other) |
| cpcv | **FAIL** OOS Sharpe −0.67 |
| monte_carlo_dd | **FAIL** 67.8% |
| cost_model | **FAIL** net/gross −0.07 (net is negative) |
| **all gates** | **FAIL** |

All 24 configs go negative (best Sharpe −0.67). The avg realistic round-trip
cost the signal's names actually incur is **0.536%** — above the ~0.50%
breakeven and above the strategy's ~0.5%/day gross edge. The ~0.59 AUC signal is
statistically real but **too small to survive real open-auction fills** — the
same "real-but-too-small after costs" wall the repo's intraday research kept
hitting.

**Decision: gap is dead. Not promoted; no live strategy built.** The flat-0.20%
pass was an idealization; realistic per-name costs flip it to a loss. This is the
disciplined framework working exactly as intended — and concrete proof that
"loosen limits / trade everything / add capital" would lose money, since even
the single best candidate loses once execution is modeled honestly.

**Net conclusion of the whole sweep:** FINNIFTY short-vol remains the only
validated, cost-surviving edge. `APPROVED_EDGES` unchanged.

## Change made this round
`validation/gates.py::evaluate_dsr` — the search-breadth floor (DSR_MIN_TRIALS)
is now a **false-positive guard**: it only fails a DSR that would otherwise
*pass* on too few configs. A DSR that fails on value now reports its real reason
instead of being masked as "search too narrow" — so the floor never hides a
genuine verdict or over-penalises a strategy with a small-but-honest search.
