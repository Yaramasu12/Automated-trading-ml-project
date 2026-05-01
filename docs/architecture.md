# Architecture

## Goal

Build one shared trading pipeline that can move from one-month backtesting to paper trading and then Angel One live execution without rewriting strategy or risk logic.

## Core Principle

Backtest, paper, and live modes use the same:

- instrument master
- expiry calendar
- feature engine
- AI agents
- strategy factory
- risk engine
- order model
- portfolio metrics

Only the broker adapter changes.

## Layers

```text
Dashboard
  -> Control API
    -> Runtime State
    -> Instrument Master
    -> Market Data
    -> Feature Engine
    -> AI Agents
    -> Strategy Factory
    -> Risk Engine
    -> Execution Router
    -> Broker Adapter
```

## Supported Instruments

- NSE/BSE cash equities
- Index futures
- Stock futures, extension-ready
- Index options
- Stock options, extension-ready
- NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY

## Expiry Handling

The instrument master creates expiry-sensitive futures and options contracts. Strategies select instruments through the master rather than hardcoding symbols. Risk rules can reject expired contracts, near-expiry oversizing, and expiry-day entries after cutoff.

## AI Agents

- Market Regime Agent: trending, breakout, high-volatility, mean-reverting
- Strategy Selection Agent: maps regime to strategy candidates
- Model Selection Agent: chooses models only when risk-adjusted performance is strong
- Capital Allocation Agent: scales allocation based on quality, not desperation
- Retraining Agent: retrains on drift or degraded metrics, not because annual target is behind

## Live Controls

Live trading requires:

- `EXECUTION_MODE=LIVE`
- `LIVE_TRADING_ENABLED=true`
- Angel One credentials
- explicit arm request
- kill switch clear
- risk checks approved

## Rollout Plan

1. Local one-month backtest across indices and liquid equities.
2. Paper trading using the same execution router.
3. Angel One live adapter with tiny capital and defined-risk strategies only.
4. Add real historical data ingestion and model training.
5. Add AWS deployment once local/paper stability is proven.

