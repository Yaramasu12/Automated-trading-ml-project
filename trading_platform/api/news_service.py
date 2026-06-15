"""NewsService — news analysis endpoints, extracted from runtime.

Phase 2 of the runtime decomposition. `news_analyze` keeps its side effects
(registering temporary event-risk windows, monitor logging, event-bus publish);
those collaborators are injected. Verbatim move.
"""
from __future__ import annotations

from typing import Any


class NewsService:
    def __init__(self, *, news_intelligence: Any, event_risk: Any, monitor: Any, event_bus: Any) -> None:
        self._news_intelligence = news_intelligence
        self._event_risk = event_risk
        self._monitor = monitor
        self._event_bus = event_bus

    def news_analyze(self, payload: dict) -> dict:
        analysis = self._news_intelligence.analyze(payload)
        if analysis.recommended_action in {"BLOCK_ENTRIES", "MANUAL_APPROVAL", "REDUCE_SIZE"}:
            self._event_risk.register_temporary_event(
                reason=analysis.reason,
                expires_at=analysis.expires_at,
                recommended_action=analysis.recommended_action,
            )
        self._monitor.record_event(
            "news_event_analyzed",
            analysis.reason,
            severity="WARN" if analysis.recommended_action != "MONITOR" else "INFO",
            metadata={
                "event_id": analysis.event_id,
                "recommended_action": analysis.recommended_action,
                "global_risk_score": analysis.global_risk_score,
            },
        )
        self._event_bus.publish("news.event_received.v1", analysis.to_dict(), "news")
        return analysis.to_dict()

    def news_events(self, limit: int = 50) -> dict:
        events = self._news_intelligence.recent_events(limit)
        return {"count": len(events), "events": events, "features": self._news_intelligence.feature_snapshot()}

    def news_features(self) -> dict:
        return self._news_intelligence.feature_snapshot()
