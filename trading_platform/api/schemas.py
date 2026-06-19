"""Pydantic response models for the FastAPI layer (Phase 3, incremental).

Until now every endpoint returned a bare ``dict``, so the OpenAPI schema was
untyped and the frontend's TS types were hand-maintained and free to drift.

These models give the busiest endpoints a typed, documented contract that shows
up in /openapi.json (and can later drive generated TS types).

IMPORTANT: every model sets ``extra="allow"`` so attaching ``response_model`` is
**non-destructive** — fields not yet declared here still pass through to the
client unchanged. That lets us adopt typing incrementally without risking that a
forgotten field silently disappears from a response. Declared fields ARE
validated, so they must match what the runtime actually returns (use ``| None``
for anything that can be null).
"""
from pydantic import BaseModel, ConfigDict, Field

from trading_platform.domain.enums import ExecutionMode, Side

# NOTE: intentionally no `from __future__ import annotations` — pydantic must see
# `state: StateResponse` as a real type, not a stringized forward reference.


# ── Request models — validate control-mutating & order inputs at the boundary ──
# These are the highest-risk endpoints, so they get the strictest validation:
# a malformed body now returns a clean 422 at the API edge instead of becoming a
# KeyError/TypeError deep inside the runtime (an opaque 500).

class ExecutionModeRequest(BaseModel):
    """POST /execution-mode — switch runtime execution mode."""
    mode: ExecutionMode


class ArmLiveRequest(BaseModel):
    """POST /live/arm — arm/disarm live order submission."""
    armed: bool


class KillSwitchRequest(BaseModel):
    """POST /kill-switch — activate/clear the kill switch."""
    active: bool
    reason: str = ""


class AgentIntervalRequest(BaseModel):
    """POST /agent/interval — set the autonomous scan interval (seconds)."""
    seconds: int = Field(gt=0, le=86_400)


class OrderRequest(BaseModel):
    """POST /orders/preview and /orders/paper — a single-leg order intent.

    extra='allow' so optional fields (metadata, priority, …) still pass through to
    the runtime's _intent_from_payload, while the core fields are validated here.
    """
    model_config = ConfigDict(extra="allow")
    symbol: str = Field(min_length=1)
    side: Side
    price: float = Field(gt=0)
    quantity: int = Field(gt=0)
    strategy_name: str = "manual"
    confidence: float = Field(default=1.0, ge=0, le=1)
    reason: str = ""


class StateResponse(BaseModel):
    """GET /state — runtime execution state."""
    model_config = ConfigDict(extra="allow")

    execution_mode: str
    live_armed: bool
    kill_switch_active: bool
    broker: str
    angel_one_configured: bool
    live_order_confirmation_ready: bool


class DataStatusResponse(BaseModel):
    """GET /data/status — instrument universe / cache status."""
    model_config = ConfigDict(extra="allow")

    instrument_source: str
    instrument_cache_path: str
    instrument_cache_exists: bool
    current_universe_count: int
    current_universe_source: str
    instrument_master_is_synthetic: bool
    instrument_last_refresh_at: str | None = None
    instrument_refresh_source: str | None = None
    auto_load_instrument_cache: bool
    historical_data_requires_credentials: bool
    angel_one_configured: bool


class HealthResponse(BaseModel):
    """GET /health — composite health snapshot.

    Top-level fields are typed; the nested sub-payloads are left as open dicts
    for now (they can become their own models incrementally).
    """
    model_config = ConfigDict(extra="allow")

    status: str
    state: StateResponse
    operational_status: str
    timestamp: str
    risk_limits: dict = {}
    scheduler: dict = {}
    event_bus: dict = {}
    manual_approval: dict = {}
    broker_capabilities: dict = {}
    exit_manager: dict = {}


class AccountStatusResponse(BaseModel):
    """GET /account/status — broker / live-trading capability flags."""
    model_config = ConfigDict(extra="allow")

    broker: str
    angel_one_configured: bool
    read_only_available: bool
    live_orders_possible: bool
    live_armed: bool
    kill_switch_active: bool


class PositionRow(BaseModel):
    """One open position with live mark-to-market P&L."""
    model_config = ConfigDict(extra="allow")

    symbol: str
    quantity: int
    side: str
    average_price: float
    mark_price: float
    unrealized_pnl: float
    realized_pnl: float
    pnl_pct: float
    live: bool


class PortfolioInfo(BaseModel):
    """Portfolio-level snapshot embedded in the positions response."""
    model_config = ConfigDict(extra="allow")

    cash: float
    equity: float
    unrealized_pnl: float
    realized_pnl: float
    drawdown: float
    peak_equity: float
    open_positions: int


class PortfolioPositionsResponse(BaseModel):
    """GET /portfolio/positions."""
    model_config = ConfigDict(extra="allow")

    count: int
    positions: list[PositionRow]
    portfolio: PortfolioInfo


# DB read wrappers — list element types are intentionally left loose (bare
# ``list``) so row-shape changes never fail response validation.
class DbTradesResponse(BaseModel):
    """GET /db/trades."""
    model_config = ConfigDict(extra="allow")
    count: int
    trades: list


class DbEquityCurveResponse(BaseModel):
    """GET /db/equity-curve."""
    model_config = ConfigDict(extra="allow")
    count: int
    curve: list


class DbDailyPnlResponse(BaseModel):
    """GET /db/daily-pnl."""
    model_config = ConfigDict(extra="allow")
    count: int
    history: list


class DbRiskEventsResponse(BaseModel):
    """GET /db/risk-events."""
    model_config = ConfigDict(extra="allow")
    count: int
    events: list


class StrategyCatalogResponse(BaseModel):
    """GET /strategies/catalog."""
    model_config = ConfigDict(extra="allow")
    count: int
    by_family: dict
    strategies: list


class InstrumentRow(BaseModel):
    """One entry of GET /universe (a top-level list of these)."""
    model_config = ConfigDict(extra="allow")
    symbol: str
    exchange: str
    segment: str
    type: str
    underlying: str | None = None
    expiry: str | None = None
    strike: float | None = None
    option_type: str | None = None
    lot_size: int


class PerformanceSummaryResponse(BaseModel):
    """GET /performance/summary.

    Lumpy sub-payloads (execution_quality, goal, policy) are kept as open dicts —
    `goal` in particular carries an enum that is best left to the JSON encoder.
    best_strategy is intentionally undeclared and passes through via extra=allow.
    """
    model_config = ConfigDict(extra="allow")
    mode: str
    lookback_days: int
    strategy_quality_scores: list
    execution_quality: dict
    goal: dict
    policy: dict


class ComplianceStatusResponse(BaseModel):
    """GET /risk/compliance."""
    model_config = ConfigDict(extra="allow")
    orders_today: int
    max_orders_per_day: int
    banned_symbols: list


class CountEventsResponse(BaseModel):
    """Shared {count, events} wrapper — GET /execution/oms/events, /monitoring/events."""
    model_config = ConfigDict(extra="allow")
    count: int
    events: list


class RiskRejectionsResponse(BaseModel):
    """GET /risk/rejections."""
    model_config = ConfigDict(extra="allow")
    count: int
    rejections: list


class AgentTradeLogResponse(BaseModel):
    """GET /agent/trades."""
    model_config = ConfigDict(extra="allow")
    count: int
    trades: list


class NewsEventsResponse(BaseModel):
    """GET /news/events."""
    model_config = ConfigDict(extra="allow")
    count: int
    events: list
    features: dict
