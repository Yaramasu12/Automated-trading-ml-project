# Audit fix — disciplined edge gating (2026-10-13)

Branch: `fix/disciplined-edge-gating`. **Paper only** — nothing here enables live
trading; `LIVE_TRADING_ENABLED` stays `false` and the edge gate runs in PAPER by
design, so the edge is proven on paper before any live discussion.

## Why "trades but no profit"

Reconstructed from the real order flow (`fill_processed` OMS events, options only,
intraday-squared-off): net ≈ **−₹17,449** over the period, with the classic
short-vol shape — small premium collected on calm days, given back on tail days
(−₹23.5k on 2026-09-07, −₹51.8k on 2026-09-11, −₹16.3k on 2026-09-14). The
project's own walk-forward study (`audit_results/2026-09-19/`) already showed the
short-vol index premium has **decayed** and **fails the full-grid DSR gate** on
every index. The system kept trading NIFTY short-vol anyway, with no live control
tying entries to whether an edge was actually present. That control is what this
change adds.

## What changed (implemented + tested)

1. **Edge gate** — `trading_platform/risk/edge_gate.py` (new, 19 tests).
   A pure, fail-closed control: an order may **open** a position only when
   - its `(strategy_family, underlying)` is on the **approved trade list**
     (`settings.APPROVED_EDGES`), and
   - that underlying's **trailing-60d median premium ≥ the pair's theta**
     (the "TRADE" verdict from `premium_regime_filter.md`; below theta = stand aside).

   Exits/reductions are never blocked. Missing or stale regime data on an entry
   ⇒ **refuse** (stand aside). It only disciplines families it has a policy for
   (today: `short_vol`); other strategies are governed by the existing gates.

2. **Wired into the authoritative gate** — `runtime._evaluate_final_execution_gate`
   now calls the edge gate for every opening entry, in all modes, and records a
   `edge_gate:<code>` rejection reason + trace. Helper `_premium_snapshot_for`
   reads the same `index_vol_premium` row the dashboard already shows.

3. **Approved list = the honest, study-backed stance** — `config.py`:
   - `APPROVED_EDGES = "short_vol:FINNIFTY:4.0"` — FINNIFTY is the one index with
     positive, cost-surviving OOS premium and a current TRADE verdict
     (trailing-60d median **4.02 ≥ 4.0** as of 2026-09-18). NIFTY (2.04),
     BANKNIFTY (1.43), MIDCPNIFTY (negative backtest) stay **off** the list until
     they clear the full-grid gates.
   - `SHORTVOL_UNDERLYING` default flipped `NIFTY → FINNIFTY` so the auto-strategy
     stops generating intents the gate would only reject.
   - `EDGE_GATE_ENABLED` (default true), `EDGE_GATE_REQUIRE_REGIME_DATA` (default true).

4. **P&L attribution fix** — `scheduler.py` `broker_filled` events now carry
   `side`/`strategy_name`/`quantity` (they were NULL; `fill_processed` already had
   them, so P&L was recoverable but inconsistent).

Net effect: the system now trades **FINNIFTY short-vol only when premium is rich**,
and **stands aside otherwise** — including standing aside on everything when the
premium feed is empty. For a decayed edge, trading rarely is the correct behavior.

## Operate it (paper)

The gate fails closed, so FINNIFTY entries stay blocked until the premium feed is
populated. Populate it once, then it self-updates via the daily job:

```
# in the running stack (TimescaleDB), run inside the api/scheduler container:
python scripts/backfill_premium_monitor.py      # fills realized_vol20/premium/trailing_median_60d
```

Verified locally: it produced FINNIFTY trailing-60d = 4.02 (TRADE), NIFTY 2.04,
BANKNIFTY 1.43 (stand aside). Confirm live via `GET /research/premium-status`.

Watch the `edge_gate:*` reasons in the OMS event stream to see the gate working:
`not_on_approved_trade_list` (NIFTY/BANKNIFTY blocked), `regime_stand_aside`
(premium thin), `regime_data_unavailable`/`_stale` (feed gap), `edge_validated`
(FINNIFTY entry allowed).

## Still recommended (not yet coded — need data/compute or are infra)

- **DSR trial-count** (finding #2): the Sept-13 promotion used `n_trials=27` and
  passed; the honest 54-trial grid fails. Fix is in the harness *caller* that
  assembles `trial_sharpes` (`scripts/run_short_vol_gates.py` /
  `validate_index_strategies.py`) — it must pass the full grid. Re-run it and let
  the results, not this default list, set `APPROVED_EDGES`.
- **P&L single-sourcing** (finding #5): the dashboard/API must read the live
  TimescaleDB, not the stale local `data/trading.db` (trades frozen 2026-09-09,
  snapshots 2026-10-01). Add a startup assert that fails loudly on SQLite fallback.
- **Stubbed AI reviews** (finding #5): `daily_ai_reviews` rows are literal
  `"test"` — either produce a real review or mark the feature disabled.
- **Tail protection** (finding #4): short-vol's losing days are tail events. Keep
  entries hedged (long wings) and size to the validated max-DD; the 2% daily-loss
  breaker only stops *new* entries, not an open book moving against you.

## Go-live criteria (for later — stays PAPER until all are met)

Do **not** flip `LIVE_TRADING_ENABLED`. When you want to revisit live, require, on
the promoted edges only: (1) the full-grid harness promotes the exact configs in
`APPROVED_EDGES`; (2) ≥ 20 paper trading days of positive net P&L within the
validated max-DD, measured from the live store; (3) the premium feed updating
daily with no staleness gaps. Until then it is a paper system.
