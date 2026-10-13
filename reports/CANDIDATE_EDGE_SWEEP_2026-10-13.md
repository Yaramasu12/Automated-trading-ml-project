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

## The gap signal: real but not promotable (yet)
Two screens agree the gap open-to-close signal is statistically real and
survives a modeled open-auction cost: `gap_calendar_volume` OOS AUC **0.594**
(beats noise + shuffle-null), `gap_backtest` "survives realistic costs." But the
cost-aware backtest reports **Sharpe 6–8** (48–145%/yr, 106 symbols, ~58
trades/day). A daily cross-sectional portfolio with Sharpe 6+ is a red flag, not
a green light: it almost certainly reflects idealized market-on-open fills with
no market-impact, slippage or short-borrow modeling. Decisively, the gated
`gap_strategy` **failed** the full DSR/PBO/CPCV/MC-DD suite in
`validate_dormant_strategies`.

**Not added to `APPROVED_EDGES`.** Promoting on a screen while the gated version
fails is the exact `n_trials=27` mistake the edge gate exists to prevent. To
promote gap honestly it needs: (1) a realistic execution model (open-auction
slippage + impact for ~58 names/day + borrow cost on shorts), and (2) a pass
through the full gate suite with a proper parameter-sweep DSR. That is a new
strategy build, not a config flip.

## Change made this round
`validation/gates.py::evaluate_dsr` — the search-breadth floor (DSR_MIN_TRIALS)
is now a **false-positive guard**: it only fails a DSR that would otherwise
*pass* on too few configs. A DSR that fails on value now reports its real reason
instead of being masked as "search too narrow" — so the floor never hides a
genuine verdict or over-penalises a strategy with a small-but-honest search.
