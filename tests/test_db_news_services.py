"""Tests for DbQueryService + NewsService (Phase 2 runtime decomposition)."""
from __future__ import annotations

import pytest

from trading_platform.api.db_query_service import DbQueryService
from trading_platform.api.news_service import NewsService


class _DB:
    def summary(self): return {"ok": True}
    def trades(self, symbol=None, execution_mode=None, limit=100): return [1, 2]
    def equity_curve(self, execution_mode=None, limit=200): return [1]
    def daily_pnl_history(self, limit=30): return [1, 2, 3]
    def recent_risk_events(self, limit=50): return []


def test_db_query_service_wrappers():
    svc = DbQueryService(db=_DB())
    assert svc.db_summary() == {"ok": True}
    assert svc.db_trades(limit=5) == {"count": 2, "trades": [1, 2]}
    assert svc.db_equity_curve()["count"] == 1
    assert svc.db_daily_pnl()["count"] == 3
    assert svc.db_risk_events() == {"count": 0, "events": []}


class _Analysis:
    recommended_action = "BLOCK_ENTRIES"
    reason = "geopolitical"
    expires_at = "2026-01-01"
    event_id = "e1"
    global_risk_score = 0.9
    def to_dict(self): return {"id": "e1"}


def test_news_analyze_fires_side_effects():
    fired = {"risk": 0, "monitor": 0, "publish": 0}
    ni = type("NI", (), {
        "analyze": lambda self, p: _Analysis(),
        "recent_events": lambda self, n: [1],
        "feature_snapshot": lambda self: {"f": 1},
    })()
    er = type("ER", (), {"register_temporary_event": lambda self, **k: fired.__setitem__("risk", 1)})()
    mon = type("M", (), {"record_event": lambda self, *a, **k: fired.__setitem__("monitor", 1)})()
    bus = type("B", (), {"publish": lambda self, *a: fired.__setitem__("publish", 1)})()
    svc = NewsService(news_intelligence=ni, event_risk=er, monitor=mon, event_bus=bus)

    assert svc.news_analyze({}) == {"id": "e1"}
    # BLOCK_ENTRIES action must register an event-risk window, log, and publish.
    assert fired == {"risk": 1, "monitor": 1, "publish": 1}
    assert svc.news_events()["features"] == {"f": 1}
    assert svc.news_features() == {"f": 1}
