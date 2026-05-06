# Automated Trading Platform — Complete Architecture

> Generated: 2026-05-06 | Branch: develop | 79 backend Python files · 10 frontend views · 55+ REST endpoints

---

## Table of Contents
1. [System Overview](#1-system-overview)
2. [Infrastructure & Deployment](#2-infrastructure--deployment)
3. [Backend — Module Map](#3-backend--module-map)
4. [Complete Trade Lifecycle Flow](#4-complete-trade-lifecycle-flow)
5. [Backend ↔ Frontend Connection](#5-backend--frontend-connection)
6. [API Endpoint Directory](#6-api-endpoint-directory)
7. [Frontend View Map](#7-frontend-view-map)
8. [Market Sessions](#8-market-sessions)
9. [What Is Fully Active vs Available-but-Idle](#9-what-is-fully-active-vs-available-but-idle)
10. [Data Persistence Map](#10-data-persistence-map)

---

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        DOCKER NETWORK                               │
│                                                                     │
│  ┌──────────────────┐    Nginx proxy     ┌──────────────────────┐  │
│  │  trading-frontend│◄──────────────────►│  trading-api         │  │
│  │  React + Nginx   │  /api/* → :8000    │  FastAPI + Uvicorn   │  │
│  │  Port 3000       │  /ws   → :8000/ws  │  Port 8000           │  │
│  └──────────────────┘                   └──────────┬───────────┘  │
│                                                     │              │
│  ┌──────────────────┐                   ┌──────────▼───────────┐  │
│  │  trading-scheduler│                  │  SQLite databases    │  │
│  │  daily_scheduler.py│                 │  ./data/trading.db   │  │
│  │  3 jobs + MCX EOD │                  │  ./data/oms_events.db│  │
│  └──────────────────┘                   └──────────────────────┘  │
│                                                                     │
│  Volume mounts (persist across rebuilds):                           │
│    ./data/          → trades, positions, snapshots, feature store   │
│    ./models/        → ML model weights (JSON)                       │
│    ./backtest_results/ → backtest output                            │
└─────────────────────────────────────────────────────────────────────┘
                              │
                    Angel One SmartAPI
                    (WebSocket + REST)
                    Live ticks · Orders · Account
```

---

## 2. Infrastructure & Deployment

### Docker Containers

| Container | Image | Port | Role |
|---|---|---|---|
| `trading-frontend` | nginx:alpine + React build | 3000 | UI + reverse proxy |
| `trading-api` | python:3.12-slim | 8000 | FastAPI backend |
| `trading-scheduler` | python:3.12-slim | — | Cron jobs (EOD, P&L) |

### Nginx Proxy Rules (hft_frontend/nginx.conf)

```
Browser → :3000/api/*  →  strip /api prefix  →  trading-api:8000/*
Browser → :3000/ws     →  WebSocket upgrade  →  trading-api:8000/ws/dashboard
Browser → :3000/*      →  React SPA          →  /usr/share/nginx/html/index.html
```

### Daily Scheduler Jobs (scripts/daily_scheduler.py)

| Time IST | Job | What it does |
|---|---|---|
| 08:55 | `instrument_refresh` | Refresh Angel One instrument master |
| 15:20 | `eod_square_off` | POST /kill-switch (equity EOD) |
| 15:35 | `daily_pnl_report` | Write daily P&L row to SQLite |
| 23:25 | `mcx_eod_square_off` | POST /execution/square-off (commodity EOD) |

---

## 3. Backend — Module Map

```
trading_platform/
│
├── api/
│   ├── app.py          ← FastAPI app, all routes, WebSocket, lifespan
│   ├── runtime.py      ← TradingRuntime (2100 lines) — master orchestrator
│   └── auth.py         ← Bearer token auth (_AuthDep)
│
├── agent/
│   ├── trading_agent.py  ← TradingAgent — autonomous scan loop (asyncio.Task)
│   └── market_hours.py   ← NSE/BSE/MCX session timing, holiday calendar
│
├── decision/
│   └── pipeline.py     ← DecisionPipeline — regime→strategy→signal→risk
│
├── strategies/
│   ├── base.py         ← Strategy ABC, StrategyExitRules, StrategyRiskEstimate
│   ├── equity.py       ← EquityMomentumStrategy, SwingTrendStrategy, GapStrategy
│   ├── derivatives.py  ← Derivatives strategies
│   └── factory.py      ← StrategyFactory — get() by name
│
├── risk/
│   ├── engine.py         ← RiskEngine — 20+ limit checks
│   ├── compliance.py     ← ComplianceGuard — pre-queue compliance
│   ├── capital_protection.py ← CapitalProtection — drawdown / margin checks
│   └── event_risk.py     ← EventRiskGuard — blocks entries near news events
│
├── execution/
│   ├── scheduler.py      ← ExecutionScheduler — asyncio.PriorityQueue
│   ├── fill_processor.py ← FillProcessor — creates Trade, updates Ledger
│   ├── lock_manager.py   ← InstrumentLockManager — per-symbol mutex
│   ├── rate_limiter.py   ← TokenBucketRateLimiter
│   ├── oms_store.py      ← OMSEventStore — SQLite event log
│   ├── emergency_square_off.py ← EmergencySquareOff
│   ├── multi_leg_manager.py    ← MultiLegOrderManager
│   ├── router.py         ← ExecutionRouter
│   └── reconciliation.py ← PositionReconciliation
│
├── exit/
│   ├── exit_manager.py   ← ExitManager — 1-second poll loop
│   └── exit_plan.py      ← ExitPlan — SL/target/trailing/expiry thresholds
│
├── portfolio/
│   └── ledger.py         ← PortfolioLedger — positions, cash, equity curve
│
├── broker/
│   ├── angel_one.py      ← AngelOneBrokerClient (live)
│   ├── simulated.py      ← SimulatedBrokerClient (paper)
│   ├── base.py           ← BrokerClient ABC
│   └── capability_registry.py ← GTT/OCO/trailing-stop support per broker
│
├── data/
│   ├── persistence.py    ← TradingDatabase — SQLite (trades, snapshots,
│   │                        positions, exit_plans, risk_events, daily_pnl)
│   ├── live_feed.py      ← LiveTickFeed — Angel One WebSocket stream
│   ├── instrument_master.py ← InstrumentMaster + build_default_universe()
│   ├── angel_one_instruments.py ← AngelOneInstrumentMasterProvider
│   ├── angel_one_history.py     ← AngelOneHistoricalDataProvider
│   ├── market_data.py    ← SyntheticDataProvider (paper/backtest)
│   └── feed_staleness.py ← Feed health monitoring
│
├── ai/
│   ├── models.py         ← RegimeClassifier, MetaModel, GARCHForecaster,
│   │                        VolatilityForecaster, SentimentAnalyzer, ModelRegistry
│   ├── feature_store.py  ← FeatureStore — per-symbol JSONL feature data
│   ├── features.py       ← Feature engineering
│   └── agents.py         ← RetrainingAgent, RiskSupervisorAgent, ModelPerformance
│
├── derivatives/
│   └── engine.py         ← ContractSelector, ExpiryCalendar, GreeksCalculator,
│                            IVSurfaceBuilder, OptionChainBuilder, RolloverPlanner
│
├── backtesting/
│   ├── engine.py         ← BacktestEngine
│   ├── evaluator.py      ← StrategyEvaluator, WalkForwardEvaluator
│   └── charges.py        ← ChargesModel (brokerage simulation)
│
├── monitoring/
│   └── ...               ← SystemMonitor, health metrics
│
├── governance/
│   └── ...               ← GovernanceDashboard, audit trail
│
├── news/
│   └── ...               ← NewsCalendar, EventRisk, news analysis
│
├── goal/
│   └── ...               ← GoalTracker, annual target progress
│
└── event_bus.py          ← InMemoryEventBus — internal pub/sub
```

---

## 4. Complete Trade Lifecycle Flow

### 4a. Signal Generation → Execution

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      EVERY SCAN CYCLE (default 5 min)                  │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │
                    TradingAgent._tick()
                               │
              ┌────────────────▼──────────────────┐
              │  Market hours check                │
              │  equity_open = is_entry_allowed()  │
              │  mcx_open = is_mcx_entry_allowed() │
              └────────────────┬──────────────────┘
                               │ active_underlyings filtered by session
                               │
              ┌────────────────▼──────────────────┐
              │  DecisionPipeline.scan()           │
              │  for each underlying:              │
              │    1. FeatureStore.get_features()  │
              │    2. RegimeClassifier.predict()   │──► regime label
              │       (TRENDING/MEAN_REVERTING/    │    (trained on forward
              │        HIGH_VOLATILITY/BREAKOUT)   │     momentum labels)
              │    3. MetaModel.select_strategy()  │──► best strategy for regime
              │    4. Strategy.generate_signal()   │──► Signal (BUY/SELL/HOLD)
              │    5. RiskEngine.check(signal)     │──► approved / rejected
              └────────────────┬──────────────────┘
                               │ approved candidates
                               │
              ┌────────────────▼──────────────────┐
              │  TradingAgent._scan_and_execute()  │
              │  Skip if symbol already in         │
              │  open_positions (no double-entry)  │
              │  Build OrderIntent → enqueue()     │
              └────────────────┬──────────────────┘
                               │
              ┌────────────────▼──────────────────┐
              │  ExecutionScheduler.enqueue()      │
              │  Pre-queue checks:                 │
              │  ① kill_switch_active? → drop      │
              │  ② OMS duplicate check            │
              │  ③ ComplianceGuard.check()         │
              │  ④ EventRiskGuard.check()          │
              │  → asyncio.PriorityQueue.put()     │
              │    Priority order:                 │
              │    1. KILL_SWITCH                  │
              │    2. EMERGENCY_EXIT               │
              │    3. STOP_LOSS                    │
              │    4. EXPIRY_EXIT                  │
              │    5. TARGET                       │
              │    6. TRAILING_STOP                │
              │    7. ENTRY  ← new trades last     │
              └────────────────┬──────────────────┘
                               │ worker dequeues
                               │
              ┌────────────────▼──────────────────┐
              │  ExecutionScheduler._process()     │
              │  ① CapitalProtection.check()       │
              │     (drawdown / margin limits)     │
              │  ② InstrumentLockManager.acquire() │
              │     (per-symbol mutex)             │
              │  ③ TokenBucketRateLimiter.acquire()│
              │  ④ broker.submit_order(intent)     │
              └────────────────┬──────────────────┘
                               │
               ┌───────────────┴──────────────────┐
               │ PAPER mode        │ LIVE mode     │
               ▼                   ▼               │
        SimulatedBroker    AngelOneBroker           │
        instant fill       SmartAPI REST            │
               │                   │               │
               └───────────────────┘               │
                               │ OrderResult (FILLED / REJECTED)
                               │
              ┌────────────────▼──────────────────┐
              │  FillProcessor.process()           │
              │  → PortfolioLedger.apply_fill()    │
              │    (updates positions, cash, P&L)  │
              │  → Trade object created            │
              └────────────────┬──────────────────┘
                               │
              ┌────────────────▼──────────────────┐
              │  TradingRuntime._on_fill()         │
              │  ① db.save_trade()                 │
              │  ② db.save_snapshot()              │
              │  ③ db.save_positions()  ◄── NEW    │
              │  ④ db.save_exit_plan()  ◄── NEW    │
              │  ⑤ ExitPlan.from_trade()           │
              │     ATR-based SL + 2.5× R:R target │
              │     70% trailing stop              │
              │     50% partial exit ladder        │
              │  ⑥ ExitManager.register(plan)      │
              │  ⑦ _agent_trade_log.append()       │
              └────────────────┬──────────────────┘
                               │
              ┌────────────────▼──────────────────┐
              │  ExitManager (1-second poll loop)  │
              │  for each active ExitPlan:         │
              │    mark = live_tick OR last_known  │
              │    OR entry_price (fallback)       │
              │    check SL / target / trail /     │
              │    expiry triggers                 │
              │    → emit exit OrderIntent         │
              │    → ExecutionScheduler.enqueue()  │
              └────────────────────────────────────┘
```

### 4b. Restart Recovery Flow (NEW)

```
Server restart / docker compose up --build
           │
           ▼
    TradingRuntime.__init__()
    TradingDatabase._init_schema()
    → creates tables if missing
    → adds DB indexes
           │
           ▼
    start_async_services()
    ├── ExecutionScheduler.start()
    ├── ExitManager.start()     ← poll loop running
    ├── LiveFeed.start()
    ├── TradingAgent.start()
    └── restore_state()
              │
              ├── db.latest_snapshot() → portfolio.cash restored
              ├── db.load_positions()  → PortfolioLedger.positions rebuilt
              └── db.load_exit_plans() → ExitManager.register() per plan
                                         stop-loss / target watches resume
```

---

## 5. Backend ↔ Frontend Connection

### Network Path

```
Browser (localhost:3000)
        │
        │  HTTP/WS to port 3000
        ▼
Nginx (trading-frontend container)
        │
        ├── /api/*  ──strip /api──►  FastAPI (trading-api:8000)
        │                            ↑ Bearer token auth on mutating endpoints
        │
        └── /ws  ───WebSocket──────►  /ws/dashboard  (trading-api:8000)
```

### REST Flow

```
React component
    └── api.ts (get / post helpers with Authorization header)
            └── fetch("/api/<endpoint>")
                    └── Nginx strips /api → FastAPI route
                            └── app.py route handler
                                    └── runtime.<method>()
                                            └── returns dict → JSON response
```

### WebSocket Flow (Real-time)

```
hft_frontend/src/ws.ts
    └── new WebSocket("ws://localhost:3000/ws")
            │  Nginx upgrades connection
            ▼
    app.py @app.websocket("/ws/dashboard")
            │  loops every 1 second:
            └── runtime.state_payload()
                    ├── portfolio snapshot (equity, cash, drawdown, positions)
                    ├── agent status (running, scan_count, last_cycle)
                    ├── scheduler stats (queue_depth, processed, rejected)
                    └── market status (OPEN / AFTER_HOURS / etc.)
            │
            ▼
    ws.ts onmessage → Zustand store updates
            │
            ▼
    React components re-render automatically
```

### Authentication

```
.env file → API_AUTH_TOKEN=<secret>
    │
    ├── Frontend: api.ts injects  Authorization: Bearer <token>  on every request
    │
    ├── Backend:  _AuthDep = Depends(verify_token)  on all mutating routes
    │             (POST, kill-switch, agent control, order submission)
    │
    └── Scheduler: os.environ.get("API_AUTH_TOKEN") → added to HTTP headers
```

---

## 6. API Endpoint Directory

### Always-On (no auth)

| Method | Path | Used by UI | Description |
|---|---|---|---|
| GET | `/health` | Dashboard | System health check |
| GET | `/state` | All views | Full runtime state |
| GET | `/universe` | Engine | Instrument universe |
| GET | `/strategies/catalog` | Strategies view | Available strategies |
| GET | `/agent/status` | Engine | Agent running state |
| GET | `/governance` | Engine | Governance dashboard |
| GET | `/monitoring/metrics` | Risk view | System metrics |
| GET | `/monitoring/events` | Risk view | System events |
| GET | `/feed/snapshot` | Engine | Live tick snapshot |
| GET | `/feed/tick/{symbol}` | Engine | Single symbol tick |
| GET | `/feed/ticks/batch` | Engine | Batch tick prices |
| GET | `/models/catalog` | Models view | ML model status |
| GET | `/data/status` | Engine | Data provider status |
| GET | `/account/status` | Account view | Angel One account |
| GET | `/regime/current` | Intelligence | Current regime |
| GET | `/news/calendar` | Intelligence | Economic calendar |
| GET | `/news/events` | Intelligence | Parsed news events |
| GET | `/risk/event-risk` | Risk view | Event risk status |
| GET | `/execution/broker-capabilities` | Execution | GTT/OCO support |
| GET | `/events/summary` | Execution | Event bus summary |
| GET | `/events/recent` | Engine | Recent events |
| WS  | `/ws/dashboard` | All views | Real-time stream |

### Requires Auth (Bearer token)

| Method | Path | Used by UI | Description |
|---|---|---|---|
| POST | `/agent/start` | Engine | Start autonomous agent |
| POST | `/agent/stop` | Engine | Stop agent |
| POST | `/agent/interval` | Engine | Change scan interval |
| GET  | `/agent/trades` | — | In-memory trade log |
| POST | `/kill-switch` | Engine | Emergency kill |
| POST | `/live/arm` | Engine | Arm live trading |
| POST | `/execution-mode` | Settings | Switch PAPER/LIVE |
| POST | `/signals/scan` | Signals | Manual signal scan |
| GET  | `/portfolio/positions` | Engine | Open positions |
| GET  | `/risk/rejections` | Engine | Risk rejection log |
| GET  | `/db/trades` | Engine, Dashboard | Persistent trade history |
| GET  | `/db/equity-curve` | Dashboard | Equity curve data |
| GET  | `/db/daily-pnl` | Dashboard | Daily P&L history |
| GET  | `/db/risk-events` | Risk view | Risk event log |
| GET  | `/db/summary` | Dashboard | DB row counts |
| POST | `/backtests/run` | Backtest | Run backtest |
| POST | `/backtests/walk-forward` | Backtest | Walk-forward test |
| POST | `/models/retraining-decision` | Models | Trigger retraining |
| POST | `/models/volatility-forecast` | Models | GARCH vol forecast |
| POST | `/models/sentiment` | Models | Sentiment analysis |
| POST | `/models/regime-classify` | Intelligence | Classify regime |
| POST | `/models/meta-rank` | Intelligence | Strategy ranking |
| POST | `/models/meta-update` | Intelligence | Update ML scores |
| POST | `/models/garch-forecast` | Models | GARCH model |
| GET  | `/account/snapshot` | Account | Account balance |
| POST | `/data/instruments/refresh` | Engine | Refresh from Angel One |
| POST | `/data/candles` | Backtest | Historical OHLC |
| GET  | `/derivatives/expiries/{u}` | Engine | Option expiries |
| GET  | `/derivatives/option-chain/{u}` | Engine | Option chain |
| POST | `/derivatives/greeks` | Engine | Options Greeks |
| POST | `/derivatives/iv-surface` | Engine | IV surface |
| POST | `/execution/enqueue` | — | Manual order enqueue |
| POST | `/execution/square-off` | Execution | Square off positions |
| POST | `/execution/multi-leg` | — | Multi-leg order |
| GET  | `/execution/scheduler/stats` | Execution | Scheduler metrics |
| GET  | `/execution/oms/events` | Execution | OMS event log |
| GET  | `/execution/manual-approvals` | Execution | Pending approvals |
| POST | `/execution/manual-approvals/{id}/approve` | Execution | Approve order |
| POST | `/execution/manual-approvals/{id}/reject` | Execution | Reject order |
| GET  | `/execution/exit-plans` | Engine | Active exit plans |
| POST | `/execution/reconcile` | — | Position reconciliation |
| POST | `/risk/supervisor-decision` | Risk | AI risk supervisor |
| GET  | `/risk/compliance` | Risk | Compliance status |
| POST | `/portfolio/target-progress` | Dashboard | Annual goal progress |
| POST | `/shadow/run` | Signals | Shadow paper run |
| POST | `/features/record` | — | Record feature vector |
| GET  | `/features/history/{symbol}` | Intelligence | Feature history |
| POST | `/news/analyze` | Intelligence | Analyze news event |
| POST | `/goal/state` | Dashboard | Update goal |
| GET  | `/performance/summary` | Dashboard | Performance metrics |

---

## 7. Frontend View Map

```
App.tsx (router)
├── /                   →  Dashboard.tsx
│   Sources: /db/equity-curve, /db/daily-pnl, /db/trades,
│            /portfolio/target-progress
│   Real-time: WebSocket (live portfolio, equity, drawdown)
│   Shows: Equity curve, daily P&L bars, recent trades,
│          open positions, streak, win rate, annual target progress
│
├── /engine             →  Engine.tsx  [MAIN TRADING VIEW]
│   Sources: /agent/status, /execution/scheduler/stats,
│            /portfolio/positions, /db/trades, /risk/rejections,
│            /governance, /execution/exit-plans, /execution/oms/events,
│            /feed/ticks/batch, /universe,
│            /derivatives/option-chain/{underlying}
│   Actions: start/stop agent, kill-switch, change interval
│   Tabs: Overview · Monitor (positions+trades) · Market (prices+options) · Debug
│
├── /signals            →  Signals.tsx
│   Sources: /signals/scan, /universe
│   Actions: POST /signals/scan (SCAN or SHADOW mode)
│   Shows: Signal candidates per underlying with regime,
│          risk decision (approved/blocked + reason)
│
├── /backtest           →  Backtest.tsx
│   Sources: /backtests/run, /data/candles, /strategies/catalog
│   Actions: Run backtest with date range + strategy config
│   Shows: Trade table (symbol/side/entry/exit/P&L), summary stats
│
├── /execution          →  Execution.tsx
│   Sources: /execution/scheduler/stats, /execution/manual-approvals,
│            /execution/oms/events, /execution/broker-capabilities,
│            /events/summary
│   Actions: Approve/reject manual approvals, Square off (GLOBAL)
│   Shows: Queue depth, OMS event log, broker capabilities
│
├── /intelligence       →  Intelligence.tsx
│   Sources: /regime/current, /models/meta-rank, /news/events,
│            /features/history/{symbol}
│   Shows: Current market regime, strategy rankings, news calendar
│
├── /models             →  Models.tsx
│   Sources: /models/catalog, /models/volatility-forecast,
│            /models/garch-forecast, /models/sentiment
│   Shows: ML model status, vol forecasts, GARCH output, sentiment
│
├── /risk               →  Risk.tsx
│   Sources: /monitoring/metrics, /monitoring/events,
│            /risk/compliance, /risk/event-risk, /db/risk-events
│   Shows: Risk limits, system health, compliance status, event risk
│
├── /strategies         →  Strategies.tsx
│   Sources: /strategies/catalog, /strategies/evaluate
│   Shows: Strategy list with backtest stats
│
└── /account            →  Account.tsx
    Sources: /account/status, /account/snapshot
    Shows: Angel One account balance, segment details
```

### Frontend State Management

```
Zustand store (store.ts)
├── runtimeState       ← from WebSocket (execution mode, kill switch)
├── livePortfolio      ← from WebSocket (live positions, equity)
├── equityCurve        ← from /db/equity-curve (Dashboard refresh)
├── dailyPnl           ← from /db/daily-pnl
├── recentTrades       ← from /db/trades
└── targetProgress     ← from /portfolio/target-progress

ws.ts (WebSocket client)
├── connects to ws://host:3000/ws
├── reconnects on disconnect
└── parses JSON → dispatches to store
```

---

## 8. Market Sessions

```
TIME (IST)    00:00          09:00 09:15          15:20 15:25 15:30          23:25 23:30
               │               │    │               │     │     │               │     │
               │               │    │               │     │     │               │     │
MCX (non-agri)  ───────────────[═══════════════════════════════]───────────────[EOD]──
                                ^                                               ^
                           MCX opens                                    commodity sq-off

NSE/BSE              ──────────[PRE]─[══════════════]─[cut]─[SQF]──────────────────────
                                       ^             ^
                                  equity open    equity sq-off

Legend:
  [═] = trading open      [PRE] = premarket (instrument refresh)
  [cut] = entry cutoff    [EOD] = forced square-off     [SQF] = equity square-off

NSE/BSE Holidays 2026:
  Jan 26 (Republic Day), Mar 25 (Holi), Apr 3 (Good Friday),
  Apr 14 (Ambedkar Jayanti), May 1 (Maharashtra Day), Aug 15 (Independence Day),
  Oct 2 (Gandhi Jayanti), Nov 4 (Diwali), Dec 25 (Christmas)
```

### Agent Scan Filter by Session

```
09:00–09:15 IST   → MCX commodities only
09:15–15:20 IST   → Equity (NSE/BSE) + MCX commodities (overlap)
15:20–15:25 IST   → MCX commodities only (equity entry cutoff passed)
15:25–15:30 IST   → Equity EOD square-off fires
15:30–23:25 IST   → MCX commodities only
23:25–23:30 IST   → MCX EOD square-off fires
23:30–09:00 IST   → All markets closed — agent sleeps
```

---

## 9. What Is Fully Active vs Available-but-Idle

### ✅ Fully Active (wired into every trade cycle)

| Component | File | Role |
|---|---|---|
| TradingAgent | agent/trading_agent.py | Drives everything — scan loop |
| DecisionPipeline | decision/pipeline.py | Regime → strategy → signal |
| RiskEngine | risk/engine.py | 20+ checks per signal |
| ComplianceGuard | risk/compliance.py | Pre-queue compliance filter |
| CapitalProtection | risk/capital_protection.py | Drawdown / margin gate |
| EventRiskGuard | risk/event_risk.py | Blocks entries near news |
| ExecutionScheduler | execution/scheduler.py | Priority queue + broker submit |
| FillProcessor | execution/fill_processor.py | Trade creation |
| InstrumentLockManager | execution/lock_manager.py | Per-symbol mutex |
| TokenBucketRateLimiter | execution/rate_limiter.py | Order rate limiting |
| OMSEventStore | execution/oms_store.py | Full audit trail |
| ExitManager | exit/exit_manager.py | SL/target/trailing watch (1s loop) |
| ExitPlan | exit/exit_plan.py | Per-position protection |
| PortfolioLedger | portfolio/ledger.py | P&L, positions, cash |
| TradingDatabase | data/persistence.py | SQLite persistence |
| InstrumentMaster | data/instrument_master.py | NSE/BSE/MCX universe |
| LiveTickFeed | data/live_feed.py | Angel One WebSocket (when configured) |
| FeatureStore | ai/feature_store.py | JSONL feature data per symbol |
| RegimeClassifier | ai/models.py | Forward-return trained regime ML |
| MetaModel | ai/models.py | Strategy scoring + selection |
| EmergencySquareOff | execution/emergency_square_off.py | Kill-switch + manual |
| InMemoryEventBus | event_bus.py | Internal pub/sub |

### ⚠️ Available — Callable via API, Not in Autonomous Loop

| Component | File | How to trigger | What's missing |
|---|---|---|---|
| GARCHForecaster | ai/models.py | POST /models/garch-forecast | Not fed into signal generation |
| VolatilityForecaster | ai/models.py | POST /models/volatility-forecast | Not in DecisionPipeline |
| SentimentAnalyzer | ai/models.py | POST /models/sentiment | Not in DecisionPipeline |
| IVSurfaceBuilder | derivatives/engine.py | POST /derivatives/iv-surface | Greeks not used for entries |
| GreeksCalculator | derivatives/engine.py | POST /derivatives/greeks | Not in risk checks |
| RolloverPlanner | derivatives/engine.py | — | Rollover not automated |
| MultiLegOrderManager | execution/multi_leg_manager.py | POST /execution/multi-leg | Strategies don't generate multi-leg |
| PositionReconciliation | execution/reconciliation.py | POST /execution/reconcile | Not on a schedule |
| RetrainingAgent | ai/agents.py | POST /models/retraining-decision | Not auto-triggered |
| RiskSupervisorAgent | ai/agents.py | POST /risk/supervisor-decision | Not in agent loop |
| WalkForwardEvaluator | backtesting/evaluator.py | POST /backtests/walk-forward | Manual only |
| NewsCalendar | news/ | GET /news/calendar | EventRiskGuard uses it |
| GoalTracker | goal/ | POST /goal/state | Manual target setting |

### ❌ Frontend Views with Limited Backend Coverage

| View | Status | Gap |
|---|---|---|
| Intelligence.tsx | Partial | Regime + strategy rank work; news features limited |
| Models.tsx | Partial | Model catalog shows status; forecasts manual only |
| Strategies.tsx | Partial | Catalog works; live P&L per strategy not tracked |
| Account.tsx | Works if Angel One configured | Paper mode returns empty |

---

## 10. Data Persistence Map

### SQLite: trading.db (volume-mounted at ./data/)

```
trades              ← every fill (paper + live + backtest)
                    indexes: timestamp, execution_mode

portfolio_snapshots ← mark-to-market snapshot after every fill
                    indexes: execution_mode

daily_pnl           ← one row per trading day (written at 15:35)
model_runs          ← regime + signal predictions logged
risk_events         ← compliance blocks, kill-switch triggers

open_positions      ← current positions (upserted on every fill)
                    PRIMARY KEY (symbol, execution_mode)
                    deleted when position closes to zero

active_exit_plans   ← active SL/target plans (inserted on entry fill)
                    PRIMARY KEY (plan_id)
                    deleted on exit fill
```

### SQLite: oms_events.db (volume-mounted at ./data/)

```
oms_events    ← full event sourcing log
              events: intent_queued, compliance_approved, compliance_rejected,
                      lock_acquired, broker_submitted, broker_filled,
                      broker_rejected, kill_switch_cancelled, exit_plan_created
```

### File system (volume-mounted)

```
./data/feature_store/<SYMBOL>.jsonl  ← per-symbol OHLCV + feature history
./models/meta_model.json             ← MetaModel strategy scores (saved on shutdown)
./models/regime_classifier.pkl       ← RegimeClassifier weights (if trained)
./backtest_results/                  ← backtest output JSON files
```

### In-Memory Only (lost on restart — but now restored from DB)

```
PortfolioLedger.positions    ← RESTORED from open_positions table
ExitManager._plans           ← RESTORED from active_exit_plans table
PortfolioLedger.cash         ← RESTORED from latest portfolio_snapshots
_agent_trade_log             ← NOT restored (in-memory session log only)
_risk_rejection_log          ← NOT restored (capped at 500 entries)
InMemoryEventBus streams     ← NOT restored (real-time only)
```

---

*Architecture document auto-generated from codebase analysis.*
*Last updated: 2026-05-06 on branch develop.*
