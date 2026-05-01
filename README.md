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
    -> Feature Engine
    -> AI Agents
    -> Strategy Factory
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

## Live Safety

Live trading requires all of the following:

- `EXECUTION_MODE=LIVE`
- `LIVE_TRADING_ENABLED=true`
- Angel One credentials in environment variables
- frontend/API arming request
- risk engine healthy
- no kill switch
- market data freshness checks passing

## Environment

See [.env.example](.env.example).
