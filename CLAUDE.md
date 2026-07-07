# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Automated trading platform for Indian markets (NSE/BSE equities, futures, options) via Angel One SmartAPI. Python 3.12 / FastAPI backend, React + TypeScript + Vite frontend. The platform normally runs in PAPER mode; LIVE trading is heavily gated (see Safety invariants).

## Commands

```bash
# Install (use the lock for reproducibility)
pip install -r requirements.txt -c constraints.lock

# Backend tests (pytest wrapper over unittest-style tests; ~640 tests, ~1-6 min)
python -m pytest tests/ -q
python -m pytest tests/test_runtime.py -q                 # one file
python -m pytest tests/ -q -k "kill_switch"               # by keyword
python -m unittest discover -s tests                      # exact CI invocation

# Run the API (port 8000)
uvicorn trading_platform.api.app:app --reload

# Frontend (in hft_frontend/; dev server on port 3000, proxies /api and /ws to :8000)
npm run dev
npm test              # vitest
npm run build         # tsc --noEmit && vite build — run this to typecheck

# Demo backtest (synthetic data, no credentials needed)
python3 scripts/run_backtest.py

# Train the return forecaster on real Angel One candles (needs .env credentials)
python scripts/train_return_forecaster.py --sweep --save
```

CI (`.github/workflows/ci.yml`) runs backend tests with `API_AUTH_REQUIRED=false`, `AUTO_START_AGENT=false`, `AUTO_START_LIVE_FEED=false`, and all `ENABLE_*_LAB` flags false. All config comes from env vars parsed in `trading_platform/config.py` (`Settings` dataclass); real credentials live in `.env` (gitignored), never in code.

## Architecture

**Composition root:** `trading_platform/api/runtime.py` — `TradingRuntime` (~3k lines) constructs and wires every subsystem. It is being incrementally decomposed into `api/*_service.py` (options, policy, quantum lab, live feed, db queries, …). When extracting more services, note that `instrument_master` is **replaced** on instrument refresh, so `_rebuild_market_engines` reconstructs dependent services; `execution_mode` / `live_armed` change without a rebuild, so services read them through injected callables.

**Decision flow (the money path):**
```
TradingAgent loop (agent/trading_agent.py, async scan cycle)
  → MasterOrchestrator.run() (orchestrator/master_orchestrator.py)
      9 nodes in order: market_intelligence → specialist_crew → neural_forecast
      → quantum_portfolio → risk_critic → profit_guard → consensus_fusion
      → goal_governor → execution_plan
      (heavy nodes run via asyncio.to_thread; failures degrade, not abort)
  → runtime.enqueue_order → final execution gate (RiskEngine re-check)
  → manual approval queue (if required) → ExecutionScheduler → broker adapter
  → on_fill: exit plan persisted to SQLite, ML feedback (meta-labeler, RAG
    reflection, champion/challenger) recorded
```
Exit management is separate: `exit/` monitors marks against persisted exit plans (restored from DB on restart) and emits square-offs. Execution mode (`BACKTEST`/`PAPER`/`LIVE`) only swaps the broker adapter — strategy, risk, and exit logic are shared.

**Two similarly-named packages:** `agent/` is the autonomous trading loop + IST market-hours logic (`now_ist()` — always use this, never naive `datetime.now()`); `agents/` is the AI-council layer (specialists, supervisor, voting, model gateway).

**Frontend:** single Zustand store (`src/store.ts`), typed API client (`src/api.ts`, all paths relative to `/api` which Vite/nginx rewrites to the backend), WebSocket hook (`src/ws.ts`) feeding the store from `/ws/dashboard` snapshots. Backend route ↔ frontend contract is tested in `tests/test_frontend_backend_contract.py`.

## Honesty discipline (the project's core rule)

This codebase repeatedly suffered from fake edge and inert components presented as intelligence. Maintain these properties:

- **Models must earn deployment.** `neural/return_forecaster.py` only activates after walk-forward validation beats a statistical noise threshold (`0.5 + max(0.02, 2·SE_null)` AUC). `RegimeClassifier.train()` refuses rule-based labels. A model that fails validation must not be saved, loaded, or served — falling back to the MA baseline is correct behavior, not a bug. As of 2026-07: daily-bar TA features showed **zero** OOS edge on real data (AUC ≈ 0.50 on 2500 days), so no `models/return_forecaster` artifact exists — deliberately.
- **Advisory ≠ safety.** LLM council is a stub, quantum is a classical fallback, and neither blocks trades. Real safety = RiskEngine, ProfitGuard, EventRiskGuard, kill switch, daily P&L circuit breaker. `api/ai_capabilities.py` logs the DEGRADED/ADVISORY status at startup — keep that report truthful when adding components.
- **Never swallow exceptions silently.** Use `note_swallowed(key, exc)` from `trading_platform/logging_safety.py` in every `except` that intentionally continues; the count is surfaced in health output.

## Safety invariants

- LIVE orders require ALL of: `EXECUTION_MODE=LIVE`, `LIVE_TRADING_ENABLED=true`, Angel One credentials, an authenticated arming request, healthy risk engine, no kill switch, fresh market data, and the confirmation phrase `I_ACCEPT_REAL_MONEY_LIVE_ORDERS`. Never weaken any link.
- Auth fails closed: `API_AUTH_REQUIRED=true` with empty `API_AUTH_TOKEN` → 503 on protected routes. The `/ws/dashboard` snapshot only includes portfolio/db/approvals fields after the connection authenticates — mirror REST auth when adding WS fields.
- Closing/square-off orders bypass position-*growth* checks (they reduce risk) but still go through the final gate.

## Landmines

- `sklearn HistGradientBoostingClassifier` deadlocks (OpenMP) on this macOS Python.framework build — use plain `GradientBoostingClassifier`.
- Runtime state (trades, equity, exit plans, OMS events) lives in SQLite under `data/` — gitignored, never commit; exit plans are restored on startup, so schema changes need migration thought.
- `backtesting/` fills at next-bar open and models slippage (half-spread + √-impact); don't introduce close-of-bar lookahead when touching it.
- Angel One API is rate-limited; historical candles are cached to `data/historical/*.csv` by the training script.
