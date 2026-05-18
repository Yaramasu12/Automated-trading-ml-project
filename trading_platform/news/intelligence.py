from __future__ import annotations

import collections
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4


_ENTITY_MAP: dict[str, dict[str, Any]] = {
    "RELIANCE": {
        "symbols": ["RELIANCE"],
        "sectors": ["energy", "oil_gas"],
        "indices": ["NIFTY"],
    },
    "HDFC": {
        "symbols": ["HDFCBANK"],
        "sectors": ["banking"],
        "indices": ["BANKNIFTY", "FINNIFTY", "NIFTY"],
    },
    "ICICI": {
        "symbols": ["ICICIBANK"],
        "sectors": ["banking"],
        "indices": ["BANKNIFTY", "FINNIFTY", "NIFTY"],
    },
    "BANK": {
        "symbols": [],
        "sectors": ["banking"],
        "indices": ["BANKNIFTY", "FINNIFTY"],
    },
    "RBI": {
        "symbols": [],
        "sectors": ["banking", "nbfc", "real_estate"],
        "indices": ["BANKNIFTY", "FINNIFTY", "NIFTY"],
    },
    "FED": {
        "symbols": [],
        "sectors": ["it", "banking", "exporters"],
        "indices": ["NIFTY", "BANKNIFTY"],
    },
    "US CPI": {
        "symbols": [],
        "sectors": ["macro"],
        "indices": ["NIFTY", "BANKNIFTY"],
    },
    "CRUDE": {
        "symbols": ["RELIANCE"],
        "sectors": ["energy", "aviation", "paints"],
        "indices": ["NIFTY"],
    },
    "CHINA": {
        "symbols": [],
        "sectors": ["metals", "chemicals", "pharma"],
        "indices": ["NIFTY"],
    },
}

_NEGATIVE_WORDS = {
    "ban",
    "crash",
    "default",
    "downgrade",
    "fall",
    "fraud",
    "loss",
    "miss",
    "probe",
    "recession",
    "reject",
    "shock",
    "war",
}

_POSITIVE_WORDS = {
    "beat",
    "growth",
    "hike",
    "profit",
    "record",
    "upgrade",
    "strong",
    "surge",
}

_HIGH_IMPACT_WORDS = {
    "rbi",
    "fed",
    "cpi",
    "budget",
    "war",
    "sebi",
    "ban",
    "default",
    "crash",
    "shock",
}


@dataclass(frozen=True)
class NewsEntityImpact:
    entity_name: str
    mapped_symbols: list[str] = field(default_factory=list)
    mapped_sectors: list[str] = field(default_factory=list)
    mapped_indices: list[str] = field(default_factory=list)
    relevance_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_name": self.entity_name,
            "mapped_symbols": self.mapped_symbols,
            "mapped_sectors": self.mapped_sectors,
            "mapped_indices": self.mapped_indices,
            "relevance_score": self.relevance_score,
        }


@dataclass(frozen=True)
class NewsAnalysis:
    event_id: str
    headline: str
    summary: str
    source: str
    source_url: str | None
    country: str
    published_at: datetime
    received_at: datetime
    event_type: str
    importance_score: float
    sentiment_score: float
    confidence_score: float
    global_risk_score: float
    recommended_action: str
    reason: str
    expires_at: datetime
    entities: list[NewsEntityImpact]
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "headline": self.headline,
            "summary": self.summary,
            "source": self.source,
            "source_url": self.source_url,
            "country": self.country,
            "published_at": self.published_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "event_type": self.event_type,
            "importance_score": self.importance_score,
            "sentiment_score": self.sentiment_score,
            "confidence_score": self.confidence_score,
            "global_risk_score": self.global_risk_score,
            "recommended_action": self.recommended_action,
            "reason": self.reason,
            "expires_at": self.expires_at.isoformat(),
            "entities": [entity.to_dict() for entity in self.entities],
            "raw_payload": self.raw_payload,
        }


class NewsIntelligence:
    """Local news normalization, entity mapping, and event-risk scoring.

    Network ingestion can be plugged in later. The current implementation lets
    the platform accept normalized news payloads, map them to market exposures,
    and emit deterministic risk recommendations for paper/shadow validation.
    """

    def __init__(self) -> None:
        self._events: collections.deque[NewsAnalysis] = collections.deque(maxlen=10_000)

    def analyze(self, payload: dict[str, Any]) -> NewsAnalysis:
        now = datetime.now(timezone.utc)
        headline = str(payload.get("headline") or payload.get("title") or "").strip()
        if not headline:
            raise ValueError("headline is required")
        summary = str(payload.get("summary") or payload.get("description") or headline)
        source = str(payload.get("source") or "manual")
        source_url = payload.get("source_url") or payload.get("url")
        country = str(payload.get("country") or "GLOBAL").upper()
        published_at = self._parse_datetime(payload.get("published_at")) or now
        text = f"{headline} {summary}"

        entities = self._map_entities(text)
        sentiment_score = self._sentiment_score(text)
        importance_score = self._importance_score(text, entities, payload)
        global_risk_score = self._global_risk_score(importance_score, sentiment_score, entities)
        recommended_action, reason = self._recommend(global_risk_score, importance_score, sentiment_score)
        event_type = self._event_type(text, country)
        expires_at = now + timedelta(hours=float(payload.get("risk_ttl_hours", 6)))

        analysis = NewsAnalysis(
            event_id=str(payload.get("event_id") or uuid4().hex),
            headline=headline,
            summary=summary,
            source=source,
            source_url=str(source_url) if source_url else None,
            country=country,
            published_at=published_at,
            received_at=now,
            event_type=event_type,
            importance_score=importance_score,
            sentiment_score=sentiment_score,
            confidence_score=min(0.99, 0.55 + 0.10 * len(entities) + 0.20 * importance_score),
            global_risk_score=global_risk_score,
            recommended_action=recommended_action,
            reason=reason,
            expires_at=expires_at,
            entities=entities,
            raw_payload=payload,
        )
        self._events.append(analysis)
        return analysis

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        import itertools
        return [event.to_dict() for event in itertools.islice(reversed(self._events), limit)]

    def feature_snapshot(self) -> dict[str, Any]:
        events = list(self._events)  # snapshot for thread safety
        now = datetime.now(timezone.utc)
        active = [event for event in events if event.expires_at > now]
        mapped_symbols = sorted(
            {
                symbol
                for event in active
                for entity in event.entities
                for symbol in entity.mapped_symbols
            }
        )
        return {
            "active_event_count": len(active),
            "breaking_news_flag": any(event.importance_score >= 0.75 for event in active),
            "global_risk_score": max((event.global_risk_score for event in active), default=0.0),
            "mapped_symbols": mapped_symbols,
            "recommended_action": self._aggregate_action(active),
        }

    def _map_entities(self, text: str) -> list[NewsEntityImpact]:
        upper = text.upper()
        entities: list[NewsEntityImpact] = []
        for name, mapping in _ENTITY_MAP.items():
            if name in upper:
                entities.append(
                    NewsEntityImpact(
                        entity_name=name,
                        mapped_symbols=list(mapping["symbols"]),
                        mapped_sectors=list(mapping["sectors"]),
                        mapped_indices=list(mapping["indices"]),
                        relevance_score=0.9 if name in upper[:80] else 0.7,
                    )
                )
        return entities

    def _sentiment_score(self, text: str) -> float:
        words = {word.strip(".,:;!?()[]{}\"'").lower() for word in text.split()}
        positives = len(words & _POSITIVE_WORDS)
        negatives = len(words & _NEGATIVE_WORDS)
        score = (positives - negatives) / max(1, positives + negatives)
        return max(-1.0, min(1.0, score))

    def _importance_score(
        self,
        text: str,
        entities: list[NewsEntityImpact],
        payload: dict[str, Any],
    ) -> float:
        if payload.get("importance_score") is not None:
            return max(0.0, min(1.0, float(payload["importance_score"])))
        words = {word.strip(".,:;!?()[]{}\"'").lower() for word in text.split()}
        impact_hits = len(words & _HIGH_IMPACT_WORDS)
        score = 0.25 + min(0.35, impact_hits * 0.12) + min(0.25, len(entities) * 0.08)
        if any(word in words for word in {"breaking", "urgent"}):
            score += 0.15
        return max(0.0, min(1.0, score))

    def _global_risk_score(
        self,
        importance_score: float,
        sentiment_score: float,
        entities: list[NewsEntityImpact],
    ) -> float:
        negative_bias = max(0.0, -sentiment_score)
        breadth = min(0.25, len({idx for e in entities for idx in e.mapped_indices}) * 0.08)
        return max(0.0, min(1.0, importance_score * 0.55 + negative_bias * 0.35 + breadth))

    def _recommend(
        self,
        global_risk_score: float,
        importance_score: float,
        sentiment_score: float,
    ) -> tuple[str, str]:
        if global_risk_score >= 0.75:
            return "BLOCK_ENTRIES", "high_global_event_risk"
        if global_risk_score >= 0.55 or (importance_score >= 0.70 and sentiment_score < 0):
            return "MANUAL_APPROVAL", "event_risk_requires_human_review"
        if global_risk_score >= 0.35:
            return "REDUCE_SIZE", "moderate_event_risk"
        return "MONITOR", "event_recorded_for_context"

    def _event_type(self, text: str, country: str) -> str:
        lower = text.lower()
        if "rbi" in lower or "fed" in lower or "cpi" in lower or "gdp" in lower:
            return "MACRO"
        if "sebi" in lower or "exchange" in lower:
            return "REGULATORY"
        if "earnings" in lower or "profit" in lower or "loss" in lower:
            return "EARNINGS"
        if country not in {"IN", "INDIA", "GLOBAL"}:
            return "GLOBAL_MARKET"
        return "MARKET_NEWS"

    def _aggregate_action(self, active: list[NewsAnalysis]) -> str:
        actions = {event.recommended_action for event in active}
        if "BLOCK_ENTRIES" in actions:
            return "BLOCK_ENTRIES"
        if "MANUAL_APPROVAL" in actions:
            return "MANUAL_APPROVAL"
        if "REDUCE_SIZE" in actions:
            return "REDUCE_SIZE"
        return "MONITOR"

    def _parse_datetime(self, raw: Any) -> datetime | None:
        if raw is None:
            return None
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        text = str(raw).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
