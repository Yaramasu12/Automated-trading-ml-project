"""Tests for the Phase 3 Pydantic response models.

These lock in that the models accept the exact shapes the runtime returns (a
wrong field type would surface as a 500 from FastAPI's response validation) and
that the models are non-destructive (extra="allow" passes unknown fields through,
so adding response_model never silently drops a field).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from trading_platform.api.schemas import (
    AgentIntervalRequest,
    ArmLiveRequest,
    ExecutionModeRequest,
    KillSwitchRequest,
    OrderRequest,
    AccountStatusResponse,
    DataStatusResponse,
    DbDailyPnlResponse,
    DbEquityCurveResponse,
    DbRiskEventsResponse,
    DbTradesResponse,
    HealthResponse,
    InstrumentRow,
    PerformanceSummaryResponse,
    PortfolioPositionsResponse,
    StateResponse,
    StrategyCatalogResponse,
    AgentTradeLogResponse,
    ComplianceStatusResponse,
    CountEventsResponse,
    NewsEventsResponse,
    RiskRejectionsResponse,
)

_STATE = {
    "execution_mode": "PAPER", "live_armed": False, "kill_switch_active": False,
    "broker": "ANGEL_ONE", "angel_one_configured": True,
    "live_order_confirmation_ready": False,
}

_DATA_STATUS = {
    "instrument_source": "angel_one", "instrument_cache_path": "/x",
    "instrument_cache_exists": False, "current_universe_count": 42,
    "current_universe_source": "runtime", "instrument_master_is_synthetic": True,
    "instrument_last_refresh_at": None, "instrument_refresh_source": None,
    "auto_load_instrument_cache": True, "historical_data_requires_credentials": True,
    "angel_one_configured": True,
}

_HEALTH = {
    "status": "healthy", "state": _STATE, "operational_status": "NORMAL",
    "timestamp": "2026-06-13T00:00:00", "risk_limits": {"a": 1},
    "scheduler": {"processed": 0}, "event_bus": {"count": 0},
    "manual_approval": {"pending": 0}, "broker_capabilities": {"gtt": True},
    "exit_manager": {"active_plans": 0},
}


def test_state_response_lossless():
    assert StateResponse.model_validate(_STATE).model_dump() == _STATE


def test_data_status_allows_null_refresh_fields():
    assert DataStatusResponse.model_validate(_DATA_STATUS).model_dump() == _DATA_STATUS


def test_health_response_nested_and_open():
    dumped = HealthResponse.model_validate(_HEALTH).model_dump()
    assert dumped["state"] == _STATE
    assert dumped["scheduler"] == {"processed": 0}
    assert dumped["status"] == "healthy"


def test_models_are_non_destructive():
    # A field not declared on the model must still pass through to the client.
    s = {**_STATE, "future_field": "x"}
    assert StateResponse.model_validate(s).model_dump()["future_field"] == "x"


def test_account_status():
    acct = {"broker": "ANGEL_ONE", "angel_one_configured": True, "read_only_available": True,
            "live_orders_possible": False, "live_armed": False, "kill_switch_active": False}
    assert AccountStatusResponse.model_validate(acct).model_dump() == acct


def test_portfolio_positions_nested():
    pos = {
        "count": 1,
        "positions": [{"symbol": "NIFTY", "quantity": 50, "side": "BUY", "average_price": 100.0,
                       "mark_price": 101.0, "unrealized_pnl": 50.0, "realized_pnl": 0.0,
                       "pnl_pct": 1.0, "live": True}],
        "portfolio": {"cash": 1000.0, "equity": 1050.0, "unrealized_pnl": 50.0, "realized_pnl": 0.0,
                      "drawdown": 0.0, "peak_equity": 1050.0, "open_positions": 1},
    }
    out = PortfolioPositionsResponse.model_validate(pos).model_dump()
    assert out["positions"][0]["symbol"] == "NIFTY"
    assert out["portfolio"]["equity"] == 1050.0


def test_db_wrappers_accept_loose_rows():
    # tuple rows, dict rows, and empty lists must all validate (rows are untyped).
    assert DbTradesResponse.model_validate({"count": 2, "trades": [{"x": 1}, {"y": 2}]}).model_dump()["count"] == 2
    assert DbEquityCurveResponse.model_validate({"count": 1, "curve": [["2026", 100]]}).model_dump()["curve"] == [["2026", 100]]
    assert DbDailyPnlResponse.model_validate({"count": 0, "history": []}).model_dump() == {"count": 0, "history": []}
    assert DbRiskEventsResponse.model_validate({"count": 1, "events": [{"e": "x"}]}).model_dump()["events"][0] == {"e": "x"}


def test_strategy_catalog():
    sc = {"count": 2, "by_family": {"trend": 1, "mr": 1}, "strategies": [{"name": "a"}, {"name": "b"}]}
    assert StrategyCatalogResponse.model_validate(sc).model_dump() == sc


def test_instrument_row_optional_option_fields():
    idx = {"symbol": "NIFTY", "exchange": "NSE", "segment": "INDEX", "type": "INDEX",
           "underlying": None, "expiry": None, "strike": None, "option_type": None, "lot_size": 50}
    opt = {"symbol": "NIFTY25JAN", "exchange": "NFO", "segment": "OPTIONS", "type": "OPT",
           "underlying": "NIFTY", "expiry": "2026-01-29", "strike": 23000.0, "option_type": "CE", "lot_size": 50}
    assert InstrumentRow.model_validate(idx).model_dump()["strike"] is None
    assert InstrumentRow.model_validate(opt).model_dump()["option_type"] == "CE"


def test_performance_summary_open_nested_and_passthrough():
    import enum

    class GoalPhase(str, enum.Enum):
        ON_TRACK = "ON_TRACK"

    perf = {"mode": "PAPER", "lookback_days": 30, "best_strategy": "x",
            "strategy_quality_scores": [{"s": 1}], "execution_quality": {"submitted_orders": 0},
            "goal": {"phase": GoalPhase.ON_TRACK, "annual_target_pct": 0.4},
            "policy": {"scaling_rule": "..."}}
    dumped = PerformanceSummaryResponse.model_validate(perf).model_dump(mode="json")
    assert dumped["best_strategy"] == "x"          # undeclared field passes through
    assert dumped["goal"]["phase"] == "ON_TRACK"   # nested enum serializes, no error


def test_wrapper_models():
    assert ComplianceStatusResponse.model_validate(
        {"orders_today": 3, "max_orders_per_day": 100, "banned_symbols": ["X"]}
    ).model_dump()["banned_symbols"] == ["X"]
    assert CountEventsResponse.model_validate({"count": 0, "events": []}).model_dump() == {"count": 0, "events": []}
    assert CountEventsResponse.model_validate({"count": 2, "events": [{"e": 1}, {"e": 2}]}).model_dump()["count"] == 2
    assert RiskRejectionsResponse.model_validate({"count": 1, "rejections": [{"r": "x"}]}).model_dump()["rejections"][0] == {"r": "x"}
    assert AgentTradeLogResponse.model_validate({"count": 1, "trades": [{"t": 1}]}).model_dump()["trades"] == [{"t": 1}]
    assert NewsEventsResponse.model_validate({"count": 1, "events": [{"n": 1}], "features": {"f": 1}}).model_dump()["features"] == {"f": 1}


# ── Request models (control-mutating + order endpoints) ───────────────────────

def test_execution_mode_request_validates_enum():
    assert ExecutionModeRequest(mode="PAPER").mode.value == "PAPER"
    with pytest.raises(ValidationError):
        ExecutionModeRequest(mode="HACKERMODE")


def test_order_request_rejects_bad_inputs():
    ok = OrderRequest(symbol="RELIANCE", side="BUY", price=100, quantity=1)
    assert ok.symbol == "RELIANCE" and ok.side.value == "BUY"
    with pytest.raises(ValidationError):
        OrderRequest(symbol="X", side="BUY", price=-5, quantity=1)     # negative price
    with pytest.raises(ValidationError):
        OrderRequest(symbol="X", side="BUY", price=5, quantity=0)      # zero quantity
    with pytest.raises(ValidationError):
        OrderRequest(symbol="X", side="HOLD", price=5, quantity=1)     # invalid side


def test_agent_interval_request_positive():
    assert AgentIntervalRequest(seconds=300).seconds == 300
    with pytest.raises(ValidationError):
        AgentIntervalRequest(seconds=0)


def test_kill_switch_and_arm_requests():
    assert KillSwitchRequest(active=True, reason="manual halt").active is True
    assert KillSwitchRequest(active=False).reason == ""
    assert ArmLiveRequest(armed=True).armed is True
