# Implementation Review: Develop Branch

Date: 2026-05-04
Branch: develop

## Executive Status

The project is on track as a local, live-capable automated trading platform, but it is not ready for unsupervised real-money trading yet. The current code now has the right spine:

React Dashboard -> FastAPI Runtime -> Universe/Expiry -> Data/Features -> Strategy Factory -> AI Agents/Models -> Decision Pipeline -> Risk Engine -> Execution Router -> Backtest/Paper/Angel One Broker

The strongest parts are the shared execution-mode toggle, Angel One abstraction, expiry-aware derivatives layer, strategy catalog, risk-gated router, paper/shadow execution path, monitoring metrics, and dashboard API coverage. The main remaining work is production-grade market data fidelity, broker reconciliation, walk-forward model training quality, deployment hardening, and live trading governance.

## What Is Implemented

1. Broker and execution modes
   - Backtest, paper, and live-capable execution are routed through shared abstractions.
   - Angel One credentials are gated separately from live arming.
   - Live orders require explicit real-money confirmation and runtime arming.

2. Universe and derivatives
   - Synthetic local universe covers NSE/BSE cash, index futures, stock futures, index options, stock options, SENSEX, BANKEX, NIFTY, BANKNIFTY, FINNIFTY, and MIDCPNIFTY style contracts.
   - Expiry calendar, contract selector, option chain builder, IV surface, Greeks, and rollover planning exist.
   - Synthetic bases are deterministic for tests; live/paper should refresh from Angel One instrument master.

3. Strategy and decision pipeline
   - Strategy factory includes equity momentum, gap, trend, futures, option spreads, straddles, strangles, condor, calendar, delta-neutral, and rollover style templates.
   - Decision scans compute features, regime, volatility forecast, strategy candidates, position quantity, and risk decisions.
   - Paper/shadow validation can now run after hours without suppressing candidates.

4. AI and agents
   - Regime, strategy selection, model selection, capital allocation, retraining, and risk supervisor agents exist.
   - GARCH-like volatility, pure-Python GARCH(1,1), sentiment baseline, feature store, meta-model, and walk-forward evaluation are implemented.
   - Regime classifier refuses rule-derived labels and records holdout metrics only for externally labelled training data.

5. Risk and governance
   - Risk engine covers live arming, kill switch, max drawdown, daily loss, strategy loss, margin utilization, position sizing, symbol/correlation exposure, OTR, naked option selling, expiry cutoff, and near-expiry gamma.
   - Closing orders are now allowed through protective states in backtest/paper so the system can reduce risk instead of trapping positions.
   - Target governance tracks run-rate and scaling, but it does not bypass drawdown limits.

6. Frontend and observability
   - React dashboard covers account, engine, signals, execution, risk, strategies, models, intelligence, and backtest views.
   - Runtime exposes monitoring metrics/events, websocket dashboard feed, OMS events, scheduler stats, live readiness, account status, risk compliance, news, goal state, and worker controls.

## Fixes Applied In This Review

1. Backtest drawdown governance
   - Tightened backtest futures margin limits for small-capital simulations.
   - Result: default one-month backtest no longer accepts oversized index futures exposure on INR 10L capital.

2. Protective order handling
   - Risk engine now lets position-reducing orders pass during backtest/paper drawdown or kill-switch states.
   - This fixes forced end-of-backtest liquidation and makes win-rate/profit-factor meaningful.

3. Shadow and live-readiness scans
   - Market-hours suppression now applies only to armed live entry generation.
   - Paper shadow runs and unarmed live scans can still produce candidates so governance/risk checks are testable after hours.

4. Deterministic synthetic derivatives
   - Synthetic NIFTY and BANKNIFTY option bases were realigned with the testable local universe.
   - Real market symbols should still come from Angel One instrument refresh before paper/live.

5. Regime classifier resilience
   - sklearn/pandas binary issues now degrade gracefully as `sklearn_unavailable` instead of crashing training.

6. Git hygiene
   - SQLite WAL/SHM runtime sidecars are ignored so local database churn is not accidentally committed.

## Blocking Gaps Before Live Money

1. Market data quality
   - Need Angel One websocket tick ingestion exercised end to end during market hours.
   - Need candle aggregation, missed-tick recovery, stale-feed detection, and market data replay.
   - Current synthetic data is useful for architecture tests, not profit validation.

2. Broker reconciliation
   - Need live position/order/funds reconciliation loop against Angel One every few seconds during trading hours.
   - Need orphan order detection, rejected order classification, partial fill handling, and broker-side square-off verification.

3. Strategy realism
   - Many option strategies are templates, not full multi-leg spread builders with exact hedge legs, margin offsets, and payoff-aware exits.
   - Defined-risk options need true paired legs before any live option spread execution.
   - Naked short options must remain blocked.

4. Model training and validation
   - Model layer is structurally present, but not yet proven with real labelled NSE/BSE datasets.
   - Need walk-forward reports per strategy/symbol/regime, benchmark comparison, drift dashboards, and promotion gates.
   - RL should remain disabled for live until simulator validation is much stronger.

5. Cost, margin, and slippage
   - Backtest charges/slippage exist, but need Angel One brokerage, STT, exchange charges, GST, stamp duty, SEBI fees, and realistic option spread slippage calibration.
   - Need margin estimation for multi-leg F&O before live order generation.

6. Compliance and operations
   - Need explicit SEBI retail algo governance checklist, audit logs, manual intervention workflow, and order-to-trade controls validated with live broker behavior.
   - Need secrets management, deployment scripts, database migrations, backups, and CloudWatch/Prometheus/Grafana wiring before AWS deployment.

## Recommended Next Plan

1. Complete Angel One live data dry run
   - Refresh instrument master.
   - Subscribe to NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, RELIANCE, and top liquid F&O stocks.
   - Confirm ticks reach dashboard, feature store, signal scan, and stale-feed monitor.

2. Upgrade paper/shadow to exchange-like simulation
   - Use live ticks but simulated fills.
   - Record expected fill, best bid/ask if available, slippage, latency, and rejection reason.
   - Run at least 5 trading days before any live order.

3. Build real multi-leg option spread execution
   - Generate complete legs for bull call, bear put, iron condor, straddle/strangle, and calendar spread.
   - Validate combined payoff, max loss, margin, hedge presence, and exit rules before routing.

4. Add live go/no-go gate
   - Require green checks for data freshness, broker auth, funds read, positions read, kill switch, square-off, paper fill rate, rejection rate, drawdown, and daily loss.
   - Do not allow live toggle to arm unless all required gates pass.

5. Controlled live launch
   - Start with tiny capital, manual approval, liquid index futures or defined-risk long-option structures only.
   - No naked option selling, no RL execution, no recovery/martingale logic.
   - Scale only after stable live profit factor, low rejection rate, controlled drawdown, and benchmark-beating paper/live shadow metrics.

