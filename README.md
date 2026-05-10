# AI Trading Platform for Indian Markets

Local-first automated trading platform for NSE/BSE equities, futures, and options with a shared pipeline for backtesting, paper trading, and Angel One live trading.

## Execution Modes

- `BACKTEST`: deterministic one-month simulations with **bid/ask half-spread + square-root market impact slippage**, charges, risk gates, expiry handling, and portfolio metrics. Signals are generated on bars `0..t-1` and orders fill at the bar `t` open (no close-of-bar lookahead).
- `PAPER`: live-like execution against a simulated broker (same slippage model as backtest).
- `LIVE`: Angel One SmartAPI execution, guarded by explicit arming, credentials, risk checks, and kill switch state.

The mode toggle only changes the broker adapter. Strategy, AI, expiry, risk, portfolio, and metrics logic stay shared across modes.

## Demo Backtest

```bash
python3 scripts/run_backtest.py
```

Runs the default 30-day multi-asset synthetic backtest. The fix for
verification N2 makes this demo produce non-degenerate metrics: the
end-of-backtest forced liquidation is no longer rejected by the position-size
cap (closing orders bypass position-growth checks because they reduce risk),
and round-trip P&L is computed via FIFO matching that handles both long and
short legs. You should see a positive `trade_count`, a real `win_rate`, and a
non-zero `profit_factor` on the demo. We do not seed the synthetic data to
guarantee profitability — the demo can still print a losing window — but it
will never silently print all-zeroes.

Option positions still open at the final bar are now liquidated at a
Black-Scholes price (intrinsic when expired, BS-priced with an IV recovered
from the entry trade when still alive) instead of the legacy
`underlying * 0.015` heuristic. See N8 in the verification report.

## Slippage Model

`SimulatedBrokerClient` applies a configurable adverse-direction price model:

- **Half-spread**: `spread_bps / 2` basis points moved against the trader (BUY pays the ask, SELL hits the bid).
- **Market impact**: `impact_bps_per_unit * sqrt(notional / impact_capacity_notional)` — a square-root participation impact.
- **Microstructure noise**: a small bounded random component that can only ever hurt the trader.

What is **not** modelled (yet): partial fills, queue position, true order book depth, broker-side rejection of large orders, intraday volatility clustering. These are documented limitations.

## Walk-Forward Validation

`WalkForwardEvaluator` runs a **fit-then-test** loop. On each window it fits a thin acceptance layer (`WalkForwardFittedParams` — confidence floor calibrated on the bottom-quartile train signals, plus train profit/Sharpe gates) on the train slice, freezes those params, then evaluates on the test slice with that filter applied. If the train fit fails the gate (no trades, negative edge, or `profit_factor < 1.0`) the test slice is **skipped** — `test_skipped: true` and zero metrics — rather than reported as if the strategy had generalised. The repo's strategies are rule-based with no learnable model weights, so we deliberately do not claim to be fitting model parameters; we are calibrating an acceptance layer on top of the rule.

## Regime Classifier

`RegimeClassifier` ships in deterministic rule-based mode by default. `train(records, label_source=...)` **refuses** to fit when `label_source="rule_based"` (or unspecified) because fitting sklearn to the same teacher's labels has no validation signal. With externally-sourced labels it performs a stratified train/test split, scores both the model and the rule baseline on the holdout, and only adopts the trained model if it beats the rule baseline. `last_train_metrics` reports the holdout numbers.

## Architecture

```text
Dashboard
  -> FastAPI Control API
    -> Market Universe + Expiry Engine
    -> Contract Selector + Option Chain + Greeks
    -> Feature Engine
    -> AI Agents
    -> Model Layer
    -> Strategy Factory
    -> Target-Aware Portfolio Engine
    -> Risk Engine
    -> Execution Router
       -> Backtest Broker
       -> Paper Broker
       -> Angel One Live Broker
```

## Local Commands

```bash
python3 -m unittest discover -s tests
npm --prefix hft_frontend test
```

### Reproducible install

The repo ships pinned direct dependencies in `requirements.txt` and a fully
resolved transitive lock in `constraints.lock`. Use the lock for a
reproducible environment:

```bash
python -m pip install -r requirements.txt -c constraints.lock
```

Without `-c constraints.lock`, pip will still pin direct dependencies but may
pull different transitive versions. Regenerate the lock after intentional
dependency changes:

```bash
python -m pip install -r requirements.txt
python -m pip freeze > constraints.lock
```

### Run the API

```bash
uvicorn trading_platform.api.app:app --reload --reload-dir trading_platform
```

Scoping reload to `trading_platform` keeps uvicorn from watching `.venv`,
`node_modules`, and generated frontend files, which otherwise can cause reload
storms during local development.

### Run the dashboard

The Vite dev server is the simplest way to drive the dashboard against a
locally running API:

```bash
cd hft_frontend
npm install
npm run dev      # http://localhost:5173
```

The dev server expects the API at `http://localhost:8000`. If you've enabled
auth, set the bearer token in the dashboard via the in-app settings (or the
`VITE_API_TOKEN` env var if you wire one up locally) — and make sure
`API_CORS_ORIGINS` includes the Vite origin.

For a production-style build:

```bash
npm --prefix hft_frontend run build
npm --prefix hft_frontend run preview
```

You can also run both API and dashboard via Docker:

```bash
docker compose up --build api          # API only
docker compose --profile frontend up   # API + frontend
```

### Continuous integration

GitHub Actions runs `python -m unittest discover -s tests` and
`npm test` + `npm run build` (in `hft_frontend/`) on every PR and on pushes
to `develop` / `main`. The workflow lives at `.github/workflows/ci.yml`.

## Data Setup

> Mutating endpoints (`POST`, plus `/account/*` and `/data/instruments/*`)
> require a bearer token. Set `API_AUTH_TOKEN` in your environment and pass
> `-H "Authorization: Bearer $API_AUTH_TOKEN"` on every example below. See
> [API Authentication](#api-authentication). Read-only `GET` endpoints
> (`/health`, `/state`, `/strategies/catalog`, `/derivatives/*`,
> `/monitoring/*`, `/models/catalog`) are open and do not need the header.

Refresh Angel One's public instrument master:

```bash
curl -X POST http://127.0.0.1:8000/data/instruments/refresh \
     -H "Authorization: Bearer $API_AUTH_TOKEN"
```

Historical candles require Angel One credentials in `.env`.

Read-only account checks:

```bash
curl http://127.0.0.1:8000/account/status
curl http://127.0.0.1:8000/account/snapshot \
     -H "Authorization: Bearer $API_AUTH_TOKEN"
```

`/account/snapshot` fetches profile, RMS/funds, holdings, positions, orders, and trades. It does not place orders.

Core architecture checks:

```bash
# Public (no auth)
curl http://127.0.0.1:8000/strategies/catalog
curl http://127.0.0.1:8000/monitoring/metrics
curl http://127.0.0.1:8000/monitoring/events
curl http://127.0.0.1:8000/models/catalog
curl http://127.0.0.1:8000/derivatives/expiries/NIFTY
curl "http://127.0.0.1:8000/derivatives/option-chain/NIFTY?spot_price=22500"

# Mutating (require Authorization: Bearer …)
curl -X POST http://127.0.0.1:8000/strategies/evaluate \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $API_AUTH_TOKEN" \
     -d '{"days":30}'
curl -X POST http://127.0.0.1:8000/signals/scan \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $API_AUTH_TOKEN" \
     -d '{"days":30,"underlyings":["NIFTY","RELIANCE"]}'
curl -X POST http://127.0.0.1:8000/shadow/run \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $API_AUTH_TOKEN" \
     -d '{"days":30,"underlyings":["RELIANCE"],"strategy_names":["equity_momentum"]}'
curl -X POST http://127.0.0.1:8000/models/volatility-forecast \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $API_AUTH_TOKEN" \
     -d '{"symbol":"NIFTY","days":30,"model_name":"garch_baseline"}'
curl -X POST http://127.0.0.1:8000/models/sentiment \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $API_AUTH_TOKEN" \
     -d '{"text":"Bank reports strong profit growth"}'
curl -X POST http://127.0.0.1:8000/portfolio/target-progress \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $API_AUTH_TOKEN" \
     -d '{}'
curl -X POST http://127.0.0.1:8000/execution-mode \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $API_AUTH_TOKEN" \
     -d '{"mode":"PAPER"}'
curl -X POST http://127.0.0.1:8000/orders/preview \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $API_AUTH_TOKEN" \
     -d '{"symbol":"RELIANCE","side":"BUY","quantity":1,"price":2800}'
curl -X POST http://127.0.0.1:8000/orders/paper \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $API_AUTH_TOKEN" \
     -d '{"symbol":"RELIANCE","side":"BUY","quantity":1,"price":2800}'
```

`/models/volatility-forecast` (and the GARCH variant) returns both an
`interval_evaluation` (in-sample, flagged as such) and a separate
`walk_forward_evaluation` block with an out-of-sample 95% interval coverage
number. The OOS coverage is the honest forecast hit-rate; the in-sample
number is a sanity check, not a forecast metric.

## API Authentication

The FastAPI control plane requires a Bearer token on every mutating endpoint and on the dashboard WebSocket. Public read-only endpoints (`/health`, `/state`, `/api/v1/health`) bypass auth.

Configuration:

```bash
# Generate a long random token
python -c "import secrets; print(secrets.token_urlsafe(48))"

# Set in .env or environment
API_AUTH_TOKEN=<the-token>
API_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
API_AUTH_REQUIRED=true
```

If `API_AUTH_REQUIRED=true` (the default) and `API_AUTH_TOKEN` is empty, the API fails closed with a `503` on protected routes. To bypass auth in pure local development, set both `API_AUTH_REQUIRED=false` and leave `API_AUTH_TOKEN` empty.

CORS no longer defaults to `*`. The wildcard-with-credentials misconfiguration that browsers reject anyway has been replaced by an explicit comma-separated allowlist (`API_CORS_ORIGINS`).

WebSocket auth: pass the token as `?token=...` on the connect URL, or send `{"action": "auth", "token": "..."}` as the first message. Mutating commands (`kill_switch`, `execution_mode`, `update_marks`) are rejected with `{"error": "unauthenticated"}` until authentication succeeds.

Example:

```bash
curl -X POST http://127.0.0.1:8000/kill-switch \
     -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $API_AUTH_TOKEN" \
     -d '{"active": false}'
```

## Live Safety

Live trading requires all of the following:

- `EXECUTION_MODE=LIVE`
- `LIVE_TRADING_ENABLED=true`
- Angel One credentials in environment variables
- frontend/API arming request (with valid `API_AUTH_TOKEN`)
- risk engine healthy
- no kill switch
- market data freshness checks passing
- explicit confirmation phrase: `I_ACCEPT_REAL_MONEY_LIVE_ORDERS`

## Environment

See [.env.example](.env.example).

Put real credentials in `.env.local` or `.env`, never in `.env.example` or source code. Both local env files are ignored by Git.
