from __future__ import annotations

import asyncio
import json
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
    """Push a JSON message to all connected dashboard clients."""
    dead: list[WebSocket] = []
    text = json.dumps(message)
    for ws in list(_ws_clients):
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


app = FastAPI(
    title="AI Trading Platform",
    description="Live-capable trading control API with backtest, paper, and Angel One live modes.",
    version="0.1.0",
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
# WebSocket — real-time dashboard push
# ---------------------------------------------------------------------------


@app.websocket("/ws/dashboard")
async def ws_dashboard(websocket: WebSocket):
    """Persistent WebSocket connection for the React dashboard.

    The server pushes a snapshot every second. The client can also send
    JSON commands: {"action": "kill_switch", "active": true/false}
    """
    await websocket.accept()
    _ws_clients.append(websocket)
    try:
        while True:
            # Push current runtime snapshot every second
            snapshot = {
                "type": "snapshot",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "state": runtime.state_payload(),
                "monitoring": runtime.monitoring_metrics(),
                "live_feed": runtime.live_feed_snapshot(),
                "db": runtime.db_summary(),
            }
            await websocket.send_text(json.dumps(snapshot))

            # Non-blocking receive — handle inbound commands if sent
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                cmd = json.loads(raw)
                if cmd.get("action") == "kill_switch":
                    runtime.set_kill_switch(bool(cmd.get("active", True)))
                elif cmd.get("action") == "execution_mode":
                    runtime.set_execution_mode(str(cmd.get("mode", "BACKTEST")))
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)


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
