# AI Trading Platform for Indian Markets

Local-first automated trading platform for NSE/BSE equities, futures, and options with a shared pipeline for backtesting, paper trading, and Angel One live trading.

## Execution Modes

- `BACKTEST`: deterministic one-month simulations with slippage, charges, risk gates, expiry handling, and portfolio metrics.
- `PAPER`: live-like execution against a simulated broker.
- `LIVE`: Angel One SmartAPI execution, guarded by explicit arming, credentials, risk checks, and kill switch state.

The mode toggle only changes the broker adapter. Strategy, AI, expiry, risk, portfolio, and metrics logic stay shared across modes.

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

Optional API run after installing requirements:

```bash
pip install -r requirements.txt
uvicorn trading_platform.api.app:app --reload
```

## Data Setup

Refresh Angel One's public instrument master:

```bash
curl -X POST http://127.0.0.1:8000/data/instruments/refresh
```

Historical candles require Angel One credentials in `.env`.

Read-only account checks:

```bash
curl http://127.0.0.1:8000/account/status
curl http://127.0.0.1:8000/account/snapshot
```

`/account/snapshot` fetches profile, RMS/funds, holdings, positions, orders, and trades. It does not place orders.

Core architecture checks:

```bash
curl http://127.0.0.1:8000/strategies/catalog
curl -X POST http://127.0.0.1:8000/strategies/evaluate -H 'Content-Type: application/json' -d '{"days":30}'
curl -X POST http://127.0.0.1:8000/signals/scan -H 'Content-Type: application/json' -d '{"days":30,"underlyings":["NIFTY","RELIANCE"]}'
curl -X POST http://127.0.0.1:8000/shadow/run -H 'Content-Type: application/json' -d '{"days":30,"underlyings":["RELIANCE"],"strategy_names":["equity_momentum"]}'
curl http://127.0.0.1:8000/monitoring/metrics
curl http://127.0.0.1:8000/monitoring/events
curl http://127.0.0.1:8000/models/catalog
curl -X POST http://127.0.0.1:8000/models/volatility-forecast -H 'Content-Type: application/json' -d '{"symbol":"NIFTY","days":30,"model_name":"garch_baseline"}'
curl -X POST http://127.0.0.1:8000/models/sentiment -H 'Content-Type: application/json' -d '{"text":"Bank reports strong profit growth"}'
curl http://127.0.0.1:8000/derivatives/expiries/NIFTY
curl "http://127.0.0.1:8000/derivatives/option-chain/NIFTY?spot_price=22500"
curl -X POST http://127.0.0.1:8000/portfolio/target-progress -H 'Content-Type: application/json' -d '{}'
curl -X POST http://127.0.0.1:8000/execution-mode -H 'Content-Type: application/json' -d '{"mode":"PAPER"}'
curl -X POST http://127.0.0.1:8000/orders/preview -H 'Content-Type: application/json' -d '{"symbol":"RELIANCE","side":"BUY","quantity":1,"price":2800}'
curl -X POST http://127.0.0.1:8000/orders/paper -H 'Content-Type: application/json' -d '{"symbol":"RELIANCE","side":"BUY","quantity":1,"price":2800}'
```

## Live Safety

Live trading requires all of the following:

- `EXECUTION_MODE=LIVE`
- `LIVE_TRADING_ENABLED=true`
- Angel One credentials in environment variables
- frontend/API arming request
- risk engine healthy
- no kill switch
- market data freshness checks passing
- explicit confirmation phrase: `I_ACCEPT_REAL_MONEY_LIVE_ORDERS`

## Environment

See [.env.example](.env.example).

Put real credentials in `.env.local` or `.env`, never in `.env.example` or source code. Both local env files are ignored by Git.
