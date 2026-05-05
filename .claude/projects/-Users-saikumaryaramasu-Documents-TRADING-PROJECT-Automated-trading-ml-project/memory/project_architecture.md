---
name: Trading platform architecture map
description: Complete end-to-end architecture of the automated trading platform (FastAPI backend + React frontend)
type: project
---

**Backend:** FastAPI on port 8000 (`trading_platform/api/app.py`). Frontend: React/Vite on port 3000 (`hft_frontend/`). Vite proxies `/api/*` → `http://localhost:8000/*` (strips `/api` prefix) and `/ws/*` → `ws://localhost:8000/*`.

**End-to-end pipeline:**
1. **Live Feed** (`data/live_feed.py`) — Angel One SmartWebSocketV2 wrapper; runs in background thread; feeds ticks into `_ticks` dict keyed by symbol. Requires Angel One credentials.
2. **Decision Pipeline** (`decision/pipeline.py`) — Per scan-cycle: fetches historical bars (Angel One history → synthetic fallback), overrides last bar with live tick, computes features (`ai/features.py`), classifies regime (ML `RegimeClassifier` if trained, else rule `MarketRegimeAgent`), ranks strategies (MetaModel if has feedback, else fixed `StrategySelectionAgent`), generates signals, evaluates risk.
3. **Trading Agent** (`agent/trading_agent.py`) — Async loop every 300s (configurable); pre-market (09:00 IST) refreshes instruments + starts live feed; EOD (15:25 IST) calls `EmergencySquareOff.square_off()`; main cycle calls `signal_scan` → enqueues approved candidates.
4. **Execution Scheduler** (`execution/scheduler.py`) — Async priority queue (EMERGENCY_EXIT > PROTECTIVE > HEDGE > ENTRY); runs compliance → capital protection → event risk → instrument lock → rate limiter → broker submit.
5. **Fill Processor** (`execution/fill_processor.py`) — Updates `PortfolioLedger`; triggers fill callbacks.
6. **Exit Manager** (`exit/exit_manager.py`) — Polls active `ExitPlan` objects against live marks; enqueues exits when stop/target/trailing/expiry triggers hit.
7. **Risk Engine** (`risk/engine.py`) — Evaluates every `OrderIntent`; checks drawdown, daily loss, position size, margin, compliance, kill switch.
8. **MetaModel** (`ai/models.py:MetaModel`) — EMA scores per (regime, strategy); updated after every exit fill via `_on_fill` in runtime; used by decision pipeline to rank strategies.
9. **RegimeClassifier** (`ai/models.py:RegimeClassifier`) — GradientBoosting trained on forward-return-derived labels every 10 agent scans; refuses to train on rule-generated labels; used by decision pipeline when `is_trained`.
10. **Feature Store** (`ai/feature_store.py`) — JSONL files per symbol in `data/feature_store/`; persists features + regime per scan for ML training.
11. **OMS Event Store** (`execution/oms_store.py`) — SQLite event log for every order lifecycle event.
12. **Database** (`data/persistence.py`) — SQLite (`data/trading.db`) for trades, equity snapshots, risk events.

**Broker:** Angel One via SmartAPI REST + SmartWebSocketV2. Paper/backtest uses `SimulatedBrokerClient`.

**Governance:** `GoalGovernance` targets 40% annual return; `PositionScaler` scales positions by goal phase; `CapitalProtection` halts on drawdown/daily-loss breach; `ComplianceGuard` caps 200 orders/day; `EventRiskGuard` blocks entries near RBI/budget events.

**Execution modes:** BACKTEST → PAPER → LIVE (agent auto) → LIVE_MANUAL_APPROVAL (every order human-gated).
