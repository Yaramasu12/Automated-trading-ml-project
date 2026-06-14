"""Phase 0 — money-path safety: safety gates must FAIL-SAFE (block), not fail-open.

Before this change, if the risk node or the event-risk check threw an exception,
the orchestrator approved the trade anyway ("fail-open"). For a system that
places real orders that is exactly backwards: an unverifiable safety check must
BLOCK the trade. These tests lock that in.
"""
from __future__ import annotations

from trading_platform.orchestrator.master_orchestrator import MasterOrchestrator
from trading_platform.orchestrator.state import OrchestratorState


def _boom(*_a, **_k):
    raise RuntimeError("simulated check failure")


class _FakeRuntime:
    """Every safety dependency raises, to prove each gate blocks on error."""
    def __init__(self):
        self.decision_pipeline = type("DP", (), {"get_regime": staticmethod(_boom)})()
        self.feature_store = type("FS", (), {"get_features": staticmethod(_boom)})()
        self.news_intelligence = type("NI", (), {"analyze": staticmethod(_boom)})()
        self.event_risk_guard = type("ERG", (), {"check": staticmethod(_boom)})()
        self.portfolio = type("PF", (), {"snapshot": staticmethod(_boom)})()
        self.settings = type("S", (), {})()


def _orch() -> MasterOrchestrator:
    return MasterOrchestrator(_FakeRuntime())


def _state() -> OrchestratorState:
    return OrchestratorState(underlying="NIFTY", symbol_universe=["NIFTY"])


def test_risk_node_blocks_when_check_errors():
    """If the risk computation throws, the node must halt with risk_approved=False."""
    result = _orch()._node_risk_critic(_state())
    assert result.halt is True
    assert result.updates.get("risk_approved") is False
    assert "check error" in (result.halt_reason or "").lower()


def test_event_risk_failure_blocks_trading():
    """If the event-risk guard throws, we cannot prove the window is safe → block."""
    result = _orch()._node_market_intelligence(_state())
    assert result.halt is True
    assert "event_risk" in (result.halt_reason or "")
    assert result.updates.get("event_risk_active") is True
