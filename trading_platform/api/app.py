from __future__ import annotations

from trading_platform.api.runtime import TradingRuntime

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
except ImportError as exc:  # pragma: no cover - exercised only without optional API deps
    raise RuntimeError("Install API dependencies with: pip install -r requirements.txt") from exc


runtime = TradingRuntime()

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
