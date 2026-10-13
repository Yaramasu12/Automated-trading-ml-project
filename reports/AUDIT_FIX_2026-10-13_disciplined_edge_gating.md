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

## Follow-up (2026-10-13, same branch): all four remaining items implemented

1. **DSR search-breadth floor** (#2). `validation/gates.py::evaluate_dsr` now
   FAILs any sweep with `n_trials < DSR_MIN_TRIALS` (default 54, the honest
   own-chain grid) regardless of the deflated value — a 27-variant sweep can no
   longer promote as it did on Sept-13. `DSR_MIN_TRIALS` is configurable. The
   authoritative validation remains `validate_index_strategies.py` (54 trials,
   own-chain IV); `APPROVED_EDGES=FINNIFTY` is justified by the pre-committed
   walk-forward (`premium_regime_filter.md`), which is stronger OOS evidence
   than any single sweep's DSR. 3 tests.
2. **P&L single-sourcing** (#5). `TradingDatabase.backend` is now public; the
   runtime refuses to start when `REQUIRE_POSTGRES` is set but it resolved to
   SQLite (stale local file) — no more silently serving frozen P&L.
   `REQUIRE_POSTGRES` defaults true when `DATABASE_URL` is a postgres URL. 3 tests.
3. **Daily AI review** (#3). Correction: the production `run_daily_ai_review`
   is already real (pulls trades/P&L/reflection stats, prompts the local model,
   scheduled 15:40) — the `"test"/"test"` rows were test-fixture data, not a
   code stub. Hardened the no-model fallback so a degraded review is labelled
   `[no local model — advisory review unavailable]` instead of reading like a
   real debrief. 1 test.
4. **Tail protection** (#4). Correction: every *live* structure is already
   defined-risk (condor/spreads/long-debit) — `strangle`/`long_straddle` in the
   executor are BUY-only, not naked. The real tail driver was defined-risk
   condors **sized too large** hitting near-max-loss on a vol spike. Added a
   **per-structure tail-loss sizing cap** (`SHORTVOL_MAX_TAIL_LOSS_PCT`, scoped
   to credit structures; opt-in in code, set to 1.5 in `.env`) that caps lots so
   one structure's defined worst-case loss stays within that % of equity. Also
   hardened the parallel `short_vol_core._select_structure` to never pick a
   naked strangle under `require_defined_risk` (default). 5 tests.

Full suite after all four: **1620 passed, 0 failed.**

## Still infra / data (require the running stack or a harness re-run)

- Re-run `validate_index_strategies.py` on fresh data and let its full-grid
  result (not the default list) confirm/adjust `APPROVED_EDGES`.
- Point the dashboard/API process at TimescaleDB in the deployment (the guard
  now enforces this when `REQUIRE_POSTGRES` is set).

## Go-live criteria (for later — stays PAPER until all are met)

Do **not** flip `LIVE_TRADING_ENABLED`. When you want to revisit live, require, on
the promoted edges only: (1) the full-grid harness promotes the exact configs in
`APPROVED_EDGES`; (2) ≥ 20 paper trading days of positive net P&L within the
validated max-DD, measured from the live store; (3) the premium feed updating
daily with no staleness gaps. Until then it is a paper system.
