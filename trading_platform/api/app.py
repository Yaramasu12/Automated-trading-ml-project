from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from trading_platform.api.runtime import TradingRuntime

try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
except ImportError as exc:  # pragma: no cover - exercised only without optional API deps
    raise RuntimeError("Install API dependencies with: pip install -r requirements.txt") from exc


runtime = TradingRuntime()

# Registry of active WebSocket connections for push broadcasting
_ws_clients: list[WebSocket] = []


async def _broadcast(message: dict) -> None:
    dead: list[WebSocket] = []
    text = json.dumps(message)
    for ws in list(_ws_clients):
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await runtime.start_async_services()
    yield
    await runtime.stop_async_services()


app = FastAPI(
    title="AI Trading Platform",
    description="Async event-driven trading platform with priority execution, exit management, and goal governance.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return runtime.health()


@app.get("/state")
def state():
    return runtime.state_payload()


@app.post("/live/arm")
def arm_live(payload: dict):
    try:
        return runtime.arm_live(bool(payload.get("armed", False)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/execution-mode")
def execution_mode(payload: dict):
    try:
        return runtime.set_execution_mode(str(payload.get("mode", "BACKTEST")))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/kill-switch")
def kill_switch(payload: dict):
    return runtime.set_kill_switch(bool(payload.get("active", True)))


@app.get("/universe")
def universe():
    return runtime.universe()


@app.get("/strategies/catalog")
def strategy_catalog():
    return runtime.strategy_catalog()


@app.post("/strategies/evaluate")
def evaluate_strategies(payload: dict):
    try:
        return runtime.evaluate_strategies(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/signals/scan")
def signal_scan(payload: dict):
    try:
        return runtime.signal_scan(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/shadow/run")
def shadow_run(payload: dict):
    try:
        return runtime.shadow_run(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/derivatives/expiries/{underlying}")
def derivative_expiries(underlying: str):
    try:
        return runtime.expiries(underlying)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/derivatives/option-chain/{underlying}")
def option_chain(underlying: str, expiry: str | None = None, spot_price: float | None = None):
    try:
        return runtime.option_chain(underlying, expiry, spot_price)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/derivatives/greeks")
def greeks(payload: dict):
    try:
        return runtime.calculate_greeks(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/data/status")
def data_status():
    return runtime.data_status()


@app.get("/account/status")
def account_status():
    return runtime.account_status()


@app.get("/account/snapshot")
def account_snapshot():
    try:
        return runtime.account_snapshot()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/data/instruments/refresh")
def refresh_instruments():
    try:
        return runtime.refresh_angel_one_instruments()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/data/instruments/load-cache")
def load_cached_instruments():
    try:
        return runtime.load_cached_angel_one_instruments()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/data/candles")
def historical_candles(payload: dict):
    try:
        return runtime.historical_candles(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/backtests/run")
def run_backtest(payload: dict):
    try:
        return runtime.run_backtest(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/orders/preview")
def preview_order(payload: dict):
    try:
        return runtime.preview_order(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/orders/paper")
def simulate_order(payload: dict):
    try:
        return runtime.simulate_order(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/models/retraining-decision")
def retraining_decision(payload: dict):
    return runtime.retraining_decision(payload)


@app.get("/models/catalog")
def model_catalog():
    return runtime.model_catalog()


@app.post("/models/volatility-forecast")
def volatility_forecast(payload: dict):
    try:
        return runtime.volatility_forecast(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/models/sentiment")
def sentiment(payload: dict):
    try:
        return runtime.sentiment(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/models/select")
def model_selection(payload: dict):
    return runtime.model_selection(payload)


@app.post("/portfolio/target-progress")
def target_progress(payload: dict):
    try:
        return runtime.target_progress(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/risk/supervisor-decision")
def supervisor_decision(payload: dict):
    return runtime.supervisor_decision(payload)


@app.get("/monitoring/metrics")
def monitoring_metrics():
    return runtime.monitoring_metrics()


@app.get("/monitoring/events")
def monitoring_events(limit: int = 20):
    return runtime.monitoring_events(limit)


# ---------------------------------------------------------------------------
# GARCH(1,1) volatility forecast
# ---------------------------------------------------------------------------


@app.post("/models/garch-forecast")
def garch_forecast(payload: dict):
    try:
        return runtime.garch_forecast(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Implied Volatility surface
# ---------------------------------------------------------------------------


@app.post("/derivatives/iv-surface")
def iv_surface(payload: dict):
    try:
        return runtime.iv_surface_compute(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Walk-forward backtesting
# ---------------------------------------------------------------------------


@app.post("/backtests/walk-forward")
def walk_forward(payload: dict):
    try:
        return runtime.walk_forward_backtest(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# ML regime classification
# ---------------------------------------------------------------------------


@app.post("/models/regime-classify")
def regime_classify(payload: dict):
    try:
        return runtime.regime_classify(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Feature store
# ---------------------------------------------------------------------------


@app.post("/features/record")
def feature_record(payload: dict):
    try:
        return runtime.feature_record(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/features/history/{symbol}")
def feature_history(symbol: str, limit: int = 100):
    return runtime.feature_history(symbol, limit)


# ---------------------------------------------------------------------------
# Meta-model strategy ranking
# ---------------------------------------------------------------------------


@app.post("/models/meta-rank")
def meta_model_rank(payload: dict):
    try:
        return runtime.meta_model_rank(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/models/meta-update")
def meta_model_update(payload: dict):
    try:
        return runtime.meta_model_update(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Database endpoints
# ---------------------------------------------------------------------------


@app.get("/db/summary")
def db_summary():
    return runtime.db_summary()


@app.get("/db/trades")
def db_trades(symbol: str | None = None, execution_mode: str | None = None, limit: int = 100):
    return runtime.db_trades(symbol=symbol, execution_mode=execution_mode, limit=limit)


@app.get("/db/equity-curve")
def db_equity_curve(execution_mode: str | None = None, limit: int = 200):
    return runtime.db_equity_curve(execution_mode=execution_mode, limit=limit)


@app.get("/db/daily-pnl")
def db_daily_pnl(limit: int = 30):
    return runtime.db_daily_pnl(limit=limit)


@app.get("/db/risk-events")
def db_risk_events(limit: int = 50):
    return runtime.db_risk_events(limit=limit)


# ---------------------------------------------------------------------------
# Live tick feed endpoints
# ---------------------------------------------------------------------------


@app.post("/feed/start")
def feed_start(payload: dict):
    try:
        return runtime.start_live_feed(payload.get("symbols", []))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/feed/stop")
def feed_stop():
    return runtime.stop_live_feed()


@app.get("/feed/snapshot")
def feed_snapshot():
    return runtime.live_feed_snapshot()


@app.get("/feed/tick/{symbol}")
def feed_tick(symbol: str):
    return runtime.latest_tick(symbol)


# ---------------------------------------------------------------------------
# Execution scheduler & OMS
# ---------------------------------------------------------------------------


@app.get("/execution/scheduler/stats")
def scheduler_stats():
    return runtime.scheduler_stats()


@app.get("/execution/oms/events")
def oms_events(limit: int = 50):
    return runtime.oms_events(limit)


@app.get("/execution/oms/order/{order_id}")
def oms_order_events(order_id: str):
    return runtime.oms_order_events(order_id)


@app.post("/execution/enqueue")
async def enqueue_order(payload: dict):
    """Enqueue an OrderIntent directly (for testing / manual orders)."""
    try:
        return await runtime.enqueue_order(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/execution/manual-approvals")
def manual_approvals():
    return runtime.manual_approval_status()


@app.post("/execution/manual-approvals/{request_id}/approve")
async def approve_manual_order(request_id: str, payload: dict | None = None):
    try:
        return await runtime.approve_order(request_id, payload or {})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/execution/manual-approvals/{request_id}/reject")
def reject_manual_order(request_id: str, payload: dict | None = None):
    try:
        return runtime.reject_order(request_id, payload or {})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/execution/multi-leg")
async def submit_multi_leg(payload: dict):
    try:
        return await runtime.submit_multi_leg(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/execution/multi-leg")
def multi_leg_orders():
    return {"orders": runtime.multi_leg_manager.all_orders()}


@app.post("/execution/square-off")
async def square_off(payload: dict):
    try:
        return await runtime.square_off(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/execution/broker-capabilities")
def broker_capabilities():
    return runtime.broker_capability_status()


@app.get("/events/summary")
def event_summary():
    return runtime.event_bus_summary()


@app.get("/events/recent")
def event_recent(limit: int = 100, stream: str | None = None):
    return runtime.event_bus_events(limit, stream)


# ---------------------------------------------------------------------------
# Exit plans
# ---------------------------------------------------------------------------


@app.get("/execution/exit-plans")
def active_exit_plans():
    return runtime.active_exit_plans()


@app.post("/execution/exit-marks")
def update_exit_marks(payload: dict):
    prices = {str(k): float(v) for k, v in payload.items()}
    return runtime.update_exit_marks(prices)


# ---------------------------------------------------------------------------
# Compliance & event risk
# ---------------------------------------------------------------------------


@app.get("/risk/compliance")
def compliance_status():
    return runtime.compliance_status()


@app.get("/risk/event-risk")
def event_risk_check(as_of: str | None = None):
    return runtime.event_risk_check(as_of)


@app.get("/news/calendar")
def economic_calendar(from_date: str | None = None, days: int = 30):
    return runtime.economic_calendar_events(from_date, days)


@app.post("/news/analyze")
def analyze_news(payload: dict):
    try:
        return runtime.news_analyze(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/news/events")
def news_events(limit: int = 50):
    return runtime.news_events(limit)


@app.get("/news/features")
def news_features():
    return runtime.news_features()


@app.get("/regime/current")
def current_regime(symbol: str = "NIFTY"):
    return runtime.current_regime(symbol)


# ---------------------------------------------------------------------------
# Goal governance
# ---------------------------------------------------------------------------


@app.post("/goal/state")
def goal_state(payload: dict):
    try:
        return runtime.goal_state(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/performance/summary")
def performance_summary(days: int = 30):
    return runtime.performance_summary({"days": days})


# ---------------------------------------------------------------------------
# Position reconciliation
# ---------------------------------------------------------------------------


@app.post("/execution/reconcile")
def reconcile_positions(payload: dict):
    broker_positions: dict[str, int] = {str(k): int(v) for k, v in payload.items()}
    return runtime.reconcile_positions(broker_positions)


# ---------------------------------------------------------------------------
# Versioned API aliases matching the implementation blueprint
# ---------------------------------------------------------------------------


@app.post("/api/v1/mode")
def api_v1_mode(payload: dict):
    return execution_mode({"mode": payload.get("mode", "BACKTEST")})


@app.get("/api/v1/health")
def api_v1_health():
    return health()


@app.get("/api/v1/orders")
def api_v1_orders(limit: int = 100):
    return runtime.oms_events(limit)


@app.post("/api/v1/orders")
async def api_v1_enqueue_order(payload: dict):
    return await enqueue_order(payload)


@app.post("/api/v1/orders/{request_id}/approve")
async def api_v1_approve_order(request_id: str, payload: dict | None = None):
    return await approve_manual_order(request_id, payload or {})


@app.post("/api/v1/orders/{request_id}/reject")
def api_v1_reject_order(request_id: str, payload: dict | None = None):
    return reject_manual_order(request_id, payload or {})


@app.post("/api/v1/square-off")
async def api_v1_square_off(payload: dict):
    return await square_off(payload)


@app.get("/api/v1/news/events")
def api_v1_news_events(limit: int = 50):
    return news_events(limit)


@app.post("/api/v1/news/analyze")
def api_v1_news_analyze(payload: dict):
    return analyze_news(payload)


@app.get("/api/v1/regime")
def api_v1_regime(symbol: str = "NIFTY"):
    return current_regime(symbol)


@app.get("/api/v1/performance")
def api_v1_performance(days: int = 30):
    return performance_summary(days)


@app.get("/api/v1/events")
def api_v1_events(limit: int = 100, stream: str | None = None):
    return event_recent(limit, stream)


# ---------------------------------------------------------------------------
# WebSocket — real-time dashboard push (updated with scheduler/exit data)
# ---------------------------------------------------------------------------


@app.websocket("/ws/dashboard")
async def ws_dashboard(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.append(websocket)
    try:
        while True:
            snapshot = {
                "type": "snapshot",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "state": runtime.state_payload(),
                "monitoring": runtime.monitoring_metrics(),
                "live_feed": runtime.live_feed_snapshot(),
                "db": runtime.db_summary(),
                "scheduler": runtime.scheduler_stats(),
                "exit_plans": runtime.exit_manager.active_plan_count,
                "manual_approvals": runtime.manual_approval_status()["pending_count"],
                "event_bus": runtime.event_bus_summary(),
            }
            await websocket.send_text(json.dumps(snapshot))
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                cmd = json.loads(raw)
                if cmd.get("action") == "kill_switch":
                    runtime.set_kill_switch(bool(cmd.get("active", True)))
                elif cmd.get("action") == "execution_mode":
                    runtime.set_execution_mode(str(cmd.get("mode", "BACKTEST")))
                elif cmd.get("action") == "update_marks":
                    marks = {str(k): float(v) for k, v in cmd.get("prices", {}).items()}
                    runtime.update_exit_marks(marks)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)
