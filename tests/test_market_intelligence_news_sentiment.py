"""Regression 2026-08-04: master_orchestrator.py's _node_market_intelligence
used to hardcode news_sentiment = 0.0 every cycle (no live news feed existed
at all — see api/ai_capabilities.py's former "no_data_source" status). It
now reads NewsIntelligence.sentiment_for(underlying); this locks that wiring
in and confirms a lookup failure still degrades to 0.0 rather than crashing
the node (same fail-safe-to-neutral behavior as before)."""
from __future__ import annotations

from trading_platform.orchestrator.master_orchestrator import MasterOrchestrator
from trading_platform.orchestrator.state import OrchestratorState


class _NonBlockingEventRisk:
    def check(self):
        return type("Result", (), {"blocked": False, "reason": ""})()


class _StubNewsIntelligence:
    def __init__(self, score: float):
        self._score = score
        self.requested_for: list[str] = []

    def sentiment_for(self, underlying: str) -> float:
        self.requested_for.append(underlying)
        return self._score


class _BoomNewsIntelligence:
    def sentiment_for(self, underlying: str) -> float:
        raise RuntimeError("gateway unreachable")


def _runtime(news_intelligence):
    rt = type("RT", (), {})()
    rt.decision_pipeline = type("DP", (), {"get_regime": lambda self, u: "trending"})()
    rt.feature_store = type("FS", (), {"get_features": lambda self, u: {}})()
    rt.news_intelligence = news_intelligence
    rt.event_risk_guard = _NonBlockingEventRisk()
    rt.portfolio = type("PF", (), {"snapshot": lambda self: {}})()
    rt.settings = type("S", (), {})()
    return rt


def _state() -> OrchestratorState:
    return OrchestratorState(underlying="RELIANCE", symbol_universe=["RELIANCE"])


def test_news_sentiment_reads_from_sentiment_for():
    stub = _StubNewsIntelligence(0.42)
    orch = MasterOrchestrator(_runtime(stub))
    result = orch._node_market_intelligence(_state())
    assert result.updates.get("news_sentiment") == 0.42
    assert stub.requested_for == ["RELIANCE"]


def test_lookup_failure_degrades_to_neutral_not_crash():
    orch = MasterOrchestrator(_runtime(_BoomNewsIntelligence()))
    result = orch._node_market_intelligence(_state())  # must not raise
    assert result.updates.get("news_sentiment") == 0.0
