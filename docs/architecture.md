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
    -> Expiry Calendar
    -> Contract Selector
    -> Option Chain Builder
    -> Greeks Calculator
    -> Market Data
    -> Feature Engine
    -> AI Agents
    -> Strategy Factory
    -> Target-Aware Portfolio Engine
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

Runtime derivative services now expose:

- expiry calendars by underlying
- option-chain grouping by expiry
- liquid strike filtering around spot
- Black-Scholes Greeks for CE/PE contracts
- rollover planning for futures/options where the strategy allows rollover

## AI Agents

- Market Regime Agent: trending, breakout, high-volatility, mean-reverting
- Strategy Selection Agent: maps regime to strategy candidates
- Model Selection Agent: chooses models only when risk-adjusted performance is strong
- Capital Allocation Agent: scales allocation based on quality, not desperation
- Retraining Agent: retrains on drift or degraded metrics, not because annual target is behind
- Risk Supervisor Agent: can reduce allocation or halt trading, never raise limits

## Model Layer

The local model layer starts with deterministic baselines so the platform can be tested without paid services or heavyweight downloads:

- EWMA volatility baseline
- GARCH-like volatility baseline
- IV/return interval coverage evaluator
- finance lexicon sentiment baseline with the same API shape as a future FinBERT service
- model registry and selection gate

Sequence models, XGBoost/LightGBM, FinBERT, and RL remain promotion candidates. They must beat the baseline in walk-forward validation, paper trading, and shadow-live metrics before they can influence live routing.

## Strategy Factory

The strategy catalog includes directional, equity, futures, and options families. Each strategy exposes signal generation, risk estimate, expected-return estimate, margin requirement, exit rules, and expiry rules. The current scaffold has 19 strategy entries, including futures trend/hedge/rollover, equity momentum/swing/gap, breakout/mean reversion, and defined-risk option structures.

The strategy evaluator can run one-month backtests for an explicit strategy list or the whole factory and produces a ranked leaderboard. The rank blends return, profit factor, Sharpe-like consistency, and drawdown penalty. This gives the strategy selection layer a measurable local launch gate before paper/shadow live.

## Decision Pipeline

The signal scan pipeline connects the architecture end to end without submitting orders:

```text
Synthetic or broker data
  -> Feature Engine
  -> Market Regime Agent
  -> Strategy Selection Agent
  -> Strategy Signal
  -> Order Intent Preview
  -> Risk Engine Decision
```

This is the local bridge to paper and shadow-live: the same candidate generation and risk preview can run in `BACKTEST`, `PAPER`, or locked `LIVE` mode, while `submitted_orders` remains zero.

## Shadow Paper Runner

The shadow runner is the next gate after signal scan. It requires `PAPER` mode, takes only approved decision candidates, routes them through the simulated broker, and records filled/rejected counts, rejection rate, average latency, executions, and portfolio state. It never calls the Angel One live broker.

## Operational Monitoring

The local monitor records runtime events, order statuses, rejection rate, average/max latency, uptime, kill-switch state, and an operational status of `HEALTHY`, `DEGRADED`, or `HALTED`. This is the local foundation for Prometheus/Grafana/CloudWatch later; the API shape can remain stable when metrics move to external infrastructure.

## Target-Aware Portfolio Engine

The annual profit target is tracked centrally rather than inside any single strategy. The tracker computes required run rate, current PnL gap, allocation bias, and a maximum scaling multiplier. It only recommends selective scaling when drawdown is controlled and strategy quality is strong.

## Expanded Risk Rules

The risk engine checks hard limits for drawdown, daily loss, per-strategy loss, position size, margin utilization, short-option exposure, near-expiry gamma, symbol exposure, correlated exposure, order count, order-to-trade ratio, naked option selling, and expiry-day entries.

## Live Controls

Live trading requires:

- `EXECUTION_MODE=LIVE`
- `LIVE_TRADING_ENABLED=true`
- Angel One credentials
- explicit arm request
- kill switch clear
- risk checks approved

The local API has a runtime mode switch for `BACKTEST`, `PAPER`, and `LIVE`. Switching to `LIVE` does not arm live orders by itself.

## Rollout Plan

1. Local one-month backtest across indices and liquid equities.
2. Paper trading using the same execution router.
3. Angel One live adapter with tiny capital and defined-risk strategies only.
4. Add real historical data ingestion and model training.
5. Add AWS deployment once local/paper stability is proven.

## Local Data Endpoints

- `GET /data/status`: reports Angel One instrument cache state and credential readiness.
- `POST /data/instruments/refresh`: downloads Angel One OpenAPI instrument master and reloads the runtime universe.
- `POST /data/instruments/load-cache`: loads the cached instrument master without network access.
- `POST /data/candles`: retrieves authenticated Angel One historical candles for a known symbol.
- `GET /strategies/catalog`: returns all strategy families and rules.
- `POST /strategies/evaluate`: runs candidate strategy backtests and returns a ranked leaderboard.
- `POST /signals/scan`: generates strategy candidates and risk decisions without submitting orders.
- `POST /shadow/run`: executes approved signal candidates through the simulated broker in `PAPER` mode only.
- `GET /monitoring/metrics`: returns uptime, order, latency, rejection, and kill-switch metrics.
- `GET /monitoring/events`: returns recent runtime events.
- `GET /models/catalog`: returns active and planned model candidates.
- `POST /models/volatility-forecast`: returns volatility forecast and interval coverage check.
- `POST /models/sentiment`: returns local sentiment baseline result.
- `POST /models/select`: applies quality gates and returns the selected model or baseline fallback.
- `POST /models/retraining-decision`: returns drift/degradation retraining decisions.
- `GET /derivatives/expiries/{underlying}`: returns available expiries.
- `GET /derivatives/option-chain/{underlying}`: returns grouped calls, puts, and strikes.
- `POST /derivatives/greeks`: calculates delta, gamma, theta, and vega.
- `POST /portfolio/target-progress`: evaluates annual target run rate and scaling bias.
- `POST /risk/supervisor-decision`: returns allow/reduce/halt decision from the supervisor agent.
- `POST /execution-mode`: changes the runtime mode without modifying source or environment files.
- `POST /orders/preview`: builds an order intent and runs risk checks without submitting.
- `POST /orders/paper`: routes through the shared execution router and simulated broker; available only in `PAPER` mode.

Historical candles require Angel One credentials. Instrument refresh uses Angel One's public OpenAPI instrument master.

## Read-Only Account Endpoints

- `GET /account/status`: reports credential readiness and whether live orders are still blocked.
- `GET /account/snapshot`: fetches Angel One profile, RMS/funds, holdings, all holdings, positions, order book, and trade book.

These endpoints are read-only. Order placement remains gated by execution mode, live flag, confirmation phrase, frontend arming, kill switch, and the risk engine.
