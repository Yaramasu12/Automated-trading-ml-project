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


@app.post("/kill-switch")
def kill_switch(payload: dict):
    return runtime.set_kill_switch(bool(payload.get("active", True)))


@app.get("/universe")
def universe():
    return runtime.universe()


@app.post("/backtests/run")
def run_backtest(payload: dict):
    try:
        return runtime.run_backtest(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/models/retraining-decision")
def retraining_decision(payload: dict):
    return runtime.retraining_decision(payload)
