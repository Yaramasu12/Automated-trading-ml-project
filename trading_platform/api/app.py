from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from trading_platform.api.auth import require_auth, verify_token
from trading_platform.api.runtime import TradingRuntime
from trading_platform.config import load_settings

try:
    from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
except ImportError as exc:  # pragma: no cover - exercised only without optional API deps
    raise RuntimeError("Install API dependencies with: pip install -r requirements.txt") from exc


runtime = TradingRuntime()
_settings = load_settings()

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

_cors_origins = list(_settings.api_cors_origins) or ["http://localhost:5173", "http://127.0.0.1:5173"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# Convenience: a Depends() handle for protected mutating endpoints.
_AuthDep = Depends(require_auth)


@app.get("/health")
def health():
    return runtime.health()


@app.get("/state")
def state():
    return runtime.state_payload()


@app.post("/live/arm", dependencies=[_AuthDep])
def arm_live(payload: dict):
    try:
        return runtime.arm_live(bool(payload.get("armed", False)))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/live/readiness")
def live_readiness():
    """Phase-1 seven-gate readiness check.

    Returns ``armed_eligible``, ``blocking_reasons``, and a per-gate
    breakdown (with evidence) so the dashboard can render the failures
    as a checklist.
    """
    return runtime.live_readiness_payload()


@app.get("/live/canary-readiness", dependencies=[_AuthDep])
def live_canary_readiness():
    return runtime.live_canary_readiness_payload()


@app.post("/execution-mode", dependencies=[_AuthDep])
def execution_mode(payload: dict):
    try:
        return runtime.set_execution_mode(str(payload.get("mode", "BACKTEST")))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/kill-switch", dependencies=[_AuthDep])
def kill_switch(payload: dict):
    active = bool(payload.get("active", True))
    reason = str(payload.get("reason", "")).strip()
    if active and not reason:
        raise HTTPException(status_code=400, detail="kill_switch_reason_required")
    try:
        return runtime.set_kill_switch(active, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/universe")
def universe():
    return runtime.universe()


@app.get("/strategies/catalog")
def strategy_catalog():
    return runtime.strategy_catalog()


@app.post("/strategies/evaluate", dependencies=[_AuthDep])
def evaluate_strategies(payload: dict):
    try:
        return runtime.evaluate_strategies(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/signals/scan", dependencies=[_AuthDep])
def signal_scan(payload: dict):
    try:
        return runtime.signal_scan(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── Autonomous Agent ──────────────────────────────────────────────────────────

@app.get("/agent/status")
def agent_status():
    return runtime.agent_status()


@app.post("/agent/start", dependencies=[_AuthDep])
async def agent_start(payload: dict | None = None):
    interval = (payload or {}).get("scan_interval")
    return runtime.start_agent(scan_interval=int(interval) if interval else None)


@app.post("/agent/stop", dependencies=[_AuthDep])
def agent_stop():
    return runtime.stop_agent()


@app.post("/agent/interval", dependencies=[_AuthDep])
def agent_set_interval(payload: dict):
    seconds = int(payload.get("seconds", 300))
    return runtime.set_agent_interval(seconds)


@app.get("/agent/trades", dependencies=[_AuthDep])
def agent_trade_log(limit: int = 100):
    return runtime.agent_trade_log(limit=limit)


@app.get("/portfolio/positions", dependencies=[_AuthDep])
def portfolio_positions():
    return runtime.portfolio_positions()


@app.get("/risk/rejections", dependencies=[_AuthDep])
def risk_rejection_log(limit: int = 100):
    return runtime.risk_rejection_log(limit=limit)


@app.get("/governance")
def governance_dashboard():
    return runtime.governance_dashboard()


@app.post("/shadow/run", dependencies=[_AuthDep])
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


@app.get("/account/snapshot", dependencies=[_AuthDep])
def account_snapshot():
    try:
        return runtime.account_snapshot()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/data/instruments/refresh", dependencies=[_AuthDep])
def refresh_instruments():
    try:
        return runtime.refresh_angel_one_instruments()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/data/instruments/load-cache", dependencies=[_AuthDep])
def load_cached_instruments():
    try:
        return runtime.load_cached_angel_one_instruments()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/data/candles", dependencies=[_AuthDep])
def historical_candles(payload: dict):
    try:
        return runtime.historical_candles(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/backtests/run", dependencies=[_AuthDep])
def run_backtest(payload: dict):
    try:
        return runtime.run_backtest(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/orders/preview", dependencies=[_AuthDep])
def preview_order(payload: dict):
    try:
        return runtime.preview_order(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/orders/paper", dependencies=[_AuthDep])
def simulate_order(payload: dict):
    try:
        return runtime.simulate_order(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/models/retraining-decision", dependencies=[_AuthDep])
def retraining_decision(payload: dict):
    return runtime.retraining_decision(payload)


@app.get("/models/catalog")
def model_catalog():
    return runtime.model_catalog()


@app.post("/models/volatility-forecast", dependencies=[_AuthDep])
def volatility_forecast(payload: dict):
    try:
        return runtime.volatility_forecast(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/models/sentiment", dependencies=[_AuthDep])
def sentiment(payload: dict):
    try:
        return runtime.sentiment(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/models/select", dependencies=[_AuthDep])
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


@app.post("/models/garch-forecast", dependencies=[_AuthDep])
def garch_forecast(payload: dict):
    try:
        return runtime.garch_forecast(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Implied Volatility surface
# ---------------------------------------------------------------------------


@app.post("/derivatives/iv-surface", dependencies=[_AuthDep])
def iv_surface(payload: dict):
    try:
        return runtime.iv_surface_compute(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Walk-forward backtesting
# ---------------------------------------------------------------------------


@app.post("/backtests/walk-forward", dependencies=[_AuthDep])
def walk_forward(payload: dict):
    try:
        return runtime.walk_forward_backtest(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# ML regime classification
# ---------------------------------------------------------------------------


@app.post("/models/regime-classify", dependencies=[_AuthDep])
def regime_classify(payload: dict):
    try:
        return runtime.regime_classify(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Feature store
# ---------------------------------------------------------------------------


@app.post("/features/record", dependencies=[_AuthDep])
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


@app.post("/models/meta-rank", dependencies=[_AuthDep])
def meta_model_rank(payload: dict):
    try:
        return runtime.meta_model_rank(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/models/meta-update", dependencies=[_AuthDep])
def meta_model_update(payload: dict):
    try:
        return runtime.meta_model_update(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Database endpoints
# ---------------------------------------------------------------------------


@app.get("/db/summary", dependencies=[_AuthDep])
def db_summary():
    return runtime.db_summary()


@app.get("/db/trades", dependencies=[_AuthDep])
def db_trades(symbol: str | None = None, execution_mode: str | None = None, limit: int = 100):
    return runtime.db_trades(symbol=symbol, execution_mode=execution_mode, limit=limit)


@app.get("/db/equity-curve", dependencies=[_AuthDep])
def db_equity_curve(execution_mode: str | None = None, limit: int = 200):
    return runtime.db_equity_curve(execution_mode=execution_mode, limit=limit)


@app.get("/db/daily-pnl", dependencies=[_AuthDep])
def db_daily_pnl(limit: int = 30):
    return runtime.db_daily_pnl(limit=limit)


@app.get("/db/risk-events", dependencies=[_AuthDep])
def db_risk_events(limit: int = 50):
    return runtime.db_risk_events(limit=limit)


# ---------------------------------------------------------------------------
# Live tick feed endpoints
# ---------------------------------------------------------------------------


@app.post("/feed/start", dependencies=[_AuthDep])
def feed_start(payload: dict):
    try:
        return runtime.start_live_feed(payload.get("symbols", []))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/feed/stop", dependencies=[_AuthDep])
def feed_stop():
    return runtime.stop_live_feed()


@app.get("/feed/snapshot")
def feed_snapshot():
    return runtime.live_feed_snapshot()


@app.get("/feed/tick/{symbol}")
def feed_tick(symbol: str):
    return runtime.latest_tick(symbol)


@app.get("/feed/ticks/batch")
def feed_ticks_batch(symbols: str = ""):
    """Return ticks for multiple symbols in one request.
    Pass symbols as comma-separated query param: ?symbols=NIFTY,BANKNIFTY,...
    """
    syms = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else []
    return {"ticks": {sym: runtime.latest_tick(sym) for sym in syms}}


@app.post("/feed/ticks/batch")
def feed_ticks_batch_post(payload: dict | None = None):
    """Return ticks for many symbols without relying on a very long URL."""
    payload = payload or {}
    symbols = payload.get("symbols") or []
    include_unavailable = bool(payload.get("include_unavailable", False))
    if isinstance(symbols, str):
        symbols = [s.strip() for s in symbols.split(",") if s.strip()]
    ticks = {
        str(sym).upper(): runtime.latest_tick(str(sym))
        for sym in symbols
    }
    if not include_unavailable:
        ticks = {sym: tick for sym, tick in ticks.items() if tick.get("available")}
    return {"ticks": ticks}


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


@app.post("/execution/enqueue", dependencies=[_AuthDep])
async def enqueue_order(payload: dict):
    """Enqueue an OrderIntent directly (for testing / manual orders)."""
    try:
        return await runtime.enqueue_order(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/execution/manual-approvals", dependencies=[_AuthDep])
def manual_approvals():
    return runtime.manual_approval_status()


@app.post("/execution/manual-approvals/{request_id}/approve", dependencies=[_AuthDep])
async def approve_manual_order(request_id: str, payload: dict | None = None):
    try:
        return await runtime.approve_order(request_id, payload or {})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/execution/manual-approvals/{request_id}/reject", dependencies=[_AuthDep])
def reject_manual_order(request_id: str, payload: dict | None = None):
    try:
        return runtime.reject_order(request_id, payload or {})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/execution/multi-leg", dependencies=[_AuthDep])
async def submit_multi_leg(payload: dict):
    try:
        return await runtime.submit_multi_leg(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/execution/multi-leg", dependencies=[_AuthDep])
def multi_leg_orders():
    return {"orders": runtime.multi_leg_manager.all_orders()}


@app.post("/execution/square-off", dependencies=[_AuthDep])
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


@app.get("/execution/exit-plans", dependencies=[_AuthDep])
def active_exit_plans():
    return runtime.active_exit_plans()


@app.post("/execution/exit-marks", dependencies=[_AuthDep])
def update_exit_marks(payload: dict):
    prices = {str(k): float(v) for k, v in payload.items()}
    return runtime.update_exit_marks(prices)


# ---------------------------------------------------------------------------
# Compliance & event risk
# ---------------------------------------------------------------------------


@app.get("/risk/compliance", dependencies=[_AuthDep])
def compliance_status():
    return runtime.compliance_status()


@app.get("/risk/event-risk")
def event_risk_check(as_of: str | None = None):
    return runtime.event_risk_check(as_of)


@app.get("/news/calendar")
def economic_calendar(from_date: str | None = None, days: int = 30):
    return runtime.economic_calendar_events(from_date, days)


@app.post("/news/analyze", dependencies=[_AuthDep])
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
    import traceback as _tb, logging as _log
    try:
        return runtime.performance_summary({"days": days})
    except Exception as exc:
        _log.getLogger(__name__).error("performance_summary error: %s\n%s", exc, _tb.format_exc())
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Position reconciliation
# ---------------------------------------------------------------------------


@app.post("/execution/reconcile", dependencies=[_AuthDep])
def reconcile_positions(payload: dict):
    broker_positions: dict[str, int] = {str(k): int(v) for k, v in payload.items()}
    return runtime.reconcile_positions(broker_positions)


# ---------------------------------------------------------------------------
# Versioned API aliases matching the implementation blueprint
# ---------------------------------------------------------------------------


@app.post("/api/v1/mode", dependencies=[_AuthDep])
def api_v1_mode(payload: dict):
    return execution_mode({"mode": payload.get("mode", "BACKTEST")})


@app.get("/api/v1/health")
def api_v1_health():
    return health()


@app.get("/api/v1/orders")
def api_v1_orders(limit: int = 100):
    return runtime.oms_events(limit)


@app.post("/api/v1/orders", dependencies=[_AuthDep])
async def api_v1_enqueue_order(payload: dict):
    return await enqueue_order(payload)


@app.post("/api/v1/orders/{request_id}/approve", dependencies=[_AuthDep])
async def api_v1_approve_order(request_id: str, payload: dict | None = None):
    return await approve_manual_order(request_id, payload or {})


@app.post("/api/v1/orders/{request_id}/reject", dependencies=[_AuthDep])
def api_v1_reject_order(request_id: str, payload: dict | None = None):
    return reject_manual_order(request_id, payload or {})


@app.post("/api/v1/square-off", dependencies=[_AuthDep])
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
    # Token may be supplied as ?token=... query param, or via the first
    # JSON message {"action": "auth", "token": "..."}. Snapshots are
    # streamed regardless, but mutating commands are rejected until the
    # connection is authenticated.
    await websocket.accept()
    query_token = websocket.query_params.get("token")
    authed = verify_token(query_token)
    _ws_clients.append(websocket)
    _last_snapshot_hash: str = ""
    _push_interval = 5.0  # push at most every 5 seconds to reduce serialization load
    try:
        while True:
            snapshot = {
                "type": "snapshot",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "authenticated": authed,
                "state": runtime.state_payload(),
                "monitoring": runtime.monitoring_metrics(),
                "live_feed": runtime.live_feed_snapshot(),
                "db": runtime.db_summary(),
                "scheduler": runtime.scheduler_stats(),
                "exit_plans": runtime.exit_manager.active_plan_count,
                "manual_approvals": runtime.manual_approval_status()["pending_count"],
                "event_bus": runtime.event_bus_summary(),
                "portfolio": runtime.portfolio_positions(),
            }
            # Only send the snapshot when content has actually changed to avoid
            # redundant serialization and bandwidth on every poll cycle.
            snapshot_text = json.dumps(snapshot)
            snapshot_hash = hashlib.md5(snapshot_text.encode(), usedforsecurity=False).hexdigest()
            if snapshot_hash != _last_snapshot_hash:
                await websocket.send_text(snapshot_text)
                _last_snapshot_hash = snapshot_hash
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=_push_interval)
                cmd = json.loads(raw)
                action = cmd.get("action")
                if action == "auth":
                    authed = verify_token(cmd.get("token"))
                    await websocket.send_text(json.dumps({"type": "auth_result", "authenticated": authed}))
                    continue
                if action in {"kill_switch", "execution_mode", "update_marks"} and not authed:
                    await websocket.send_text(json.dumps({"type": "error", "error": "unauthenticated", "action": action}))
                    continue
                if action == "kill_switch":
                    runtime.set_kill_switch(bool(cmd.get("active", True)))
                elif action == "execution_mode":
                    runtime.set_execution_mode(str(cmd.get("mode", "BACKTEST")))
                elif action == "update_marks":
                    marks = {str(k): float(v) for k, v in cmd.get("prices", {}).items()}
                    runtime.update_exit_marks(marks)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)


# ── Phase 8: High-End AI / Quantum / Neural API endpoints ─────────────────────

@app.post("/high-end/scan", dependencies=[_AuthDep])
def high_end_scan(payload: dict):
    """Run full AI council + neural + quantum advisory scan."""
    return runtime.high_end_signal_scan(payload)


# ── Traces ─────────────────────────────────────────────────────────────────────

@app.get("/traces/{trace_id}", dependencies=[_AuthDep])
def get_trace(trace_id: str):
    trace = runtime.get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace_not_found")
    return trace


@app.get("/traces/{trace_id}/replay", dependencies=[_AuthDep])
def get_trace_replay(trace_id: str):
    replay = runtime.trace_replay(trace_id)
    if replay is None:
        raise HTTPException(status_code=404, detail="trace_not_found")
    return replay


@app.get("/traces", dependencies=[_AuthDep])
def list_traces(limit: int = 50):
    return runtime.list_traces(max_traces=limit)


# ── AI Council ─────────────────────────────────────────────────────────────────

@app.get("/ai-council/status")
def ai_council_status():
    return runtime.ai_council_status()


@app.get("/ai-council/decisions", dependencies=[_AuthDep])
def ai_council_decisions(limit: int = 20):
    traces = list(runtime.trace_store.iter_recent(limit))
    decisions = [
        t.get("agent_outputs", [])
        for t in traces
        if t.get("agent_outputs")
    ]
    return {"count": len(decisions), "decisions": decisions[:limit]}


@app.post("/ai-council/preview", dependencies=[_AuthDep])
def ai_council_preview(payload: dict):
    return runtime.ai_council_preview(payload)


# ── Neural Lab ─────────────────────────────────────────────────────────────────

@app.get("/neural/status")
def neural_status():
    return runtime.neural_status()


@app.get("/neural/meta-labeler")
def meta_labeler_status():
    return runtime.meta_labeler_status()


@app.get("/paper/learning-journal", dependencies=[_AuthDep])
def paper_learning_journal(limit: int = 50, trace_id: str | None = None):
    return runtime.paper_learning_journal_status(limit=limit, trace_id=trace_id)


@app.post("/neural/predict-preview", dependencies=[_AuthDep])
def neural_predict_preview(payload: dict):
    symbols = payload.get("symbols", ["NIFTY"])
    from trading_platform.trace.ids import new_trace_id
    trace_id = new_trace_id("npreview")
    bundle = runtime._neural_service.predict(trace_id, symbols, {})
    return bundle.to_dict()


# ── Quantum Lab ────────────────────────────────────────────────────────────────

@app.get("/quantum/status")
def quantum_status():
    return runtime.quantum_status()


@app.get("/quantum/kernel/status")
def quantum_kernel_status():
    return runtime.quantum_kernel_status()


@app.post("/quantum/optimize-preview", dependencies=[_AuthDep])
def quantum_optimize_preview(payload: dict):
    return runtime.quantum_optimize_preview(payload)


@app.get("/quantum/results", dependencies=[_AuthDep])
def quantum_results(limit: int = 20):
    traces = list(runtime.trace_store.iter_recent(limit))
    results = [
        {"trace_id": t.get("trace_id"), "quantum_result_id": t.get("quantum_result_id")}
        for t in traces
        if t.get("quantum_result_id")
    ]
    return {"count": len(results), "results": results}


# ── Goal Governor ──────────────────────────────────────────────────────────────

@app.get("/goal-governor/status")
def goal_governor_status():
    return runtime.goal_governor_status()


# ── Policy management ──────────────────────────────────────────────────────────

@app.get("/policies", dependencies=[_AuthDep])
def list_policies():
    return runtime.list_policies()


@app.post("/policies/promote", dependencies=[_AuthDep])
def promote_policy(payload: dict):
    result = runtime.promote_policy(payload)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/policies/rollback", dependencies=[_AuthDep])
def rollback_policy(payload: dict):
    result = runtime.rollback_policy(payload)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


# ── MARL lab endpoints ─────────────────────────────────────────────────────────

@app.get("/marl/status")
def marl_status():
    return runtime.marl_status()


@app.post("/marl/advisory-preview", dependencies=[_AuthDep])
def marl_advisory_preview(payload: dict):
    return runtime.marl_advisory_preview(payload)
