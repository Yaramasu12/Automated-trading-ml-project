from __future__ import annotations

import collections
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from trading_platform.ai.models import SentimentAnalyzer
from trading_platform.logging_safety import note_swallowed


def _eq(symbol: str, *, sectors: list[str] | None = None, indices: list[str] | None = None) -> dict[str, Any]:
    return {"symbols": [symbol], "sectors": sectors or [], "indices": indices or ["NIFTY"]}


_BANK_INDICES = ["BANKNIFTY", "FINNIFTY", "NIFTY"]

# Ticker/common-name -> scan-universe symbol, sector, index mapping. Covers
# trading_agent.py's EQUITY_UNDERLYINGS/COMMODITY_UNDERLYINGS (expanded
# 2026-08-04 from the original ~9 sparse macro-only entries — real headlines
# about most scanned symbols were silently unmatched). Keys are the natural
# ALL-CAPS text a headline would use (often the company name, not the
# ticker, where they differ — e.g. "TATA MOTORS" for TMPV) since _map_entities
# does a plain substring match against the uppercased headline+summary.
_ENTITY_MAP: dict[str, dict[str, Any]] = {
    "RELIANCE": _eq("RELIANCE", sectors=["energy", "oil_gas"]),
    "TCS": _eq("TCS", sectors=["it"]),
    "TATA CONSULTANCY": _eq("TCS", sectors=["it"]),
    "INFOSYS": _eq("INFY", sectors=["it"]),
    "HDFC BANK": _eq("HDFCBANK", sectors=["banking"], indices=_BANK_INDICES),
    "HDFCBANK": _eq("HDFCBANK", sectors=["banking"], indices=_BANK_INDICES),
    "ICICI BANK": _eq("ICICIBANK", sectors=["banking"], indices=_BANK_INDICES),
    "ICICIBANK": _eq("ICICIBANK", sectors=["banking"], indices=_BANK_INDICES),
    "SBI": _eq("SBIN", sectors=["banking"], indices=_BANK_INDICES),
    "STATE BANK OF INDIA": _eq("SBIN", sectors=["banking"], indices=_BANK_INDICES),
    "WIPRO": _eq("WIPRO", sectors=["it"]),
    "KOTAK": _eq("KOTAKBANK", sectors=["banking"], indices=_BANK_INDICES),
    "AXIS BANK": _eq("AXISBANK", sectors=["banking"], indices=_BANK_INDICES),
    "MARUTI": _eq("MARUTI", sectors=["auto"]),
    "SUN PHARMA": _eq("SUNPHARMA", sectors=["pharma"]),
    "TATA MOTORS": _eq("TMPV", sectors=["auto"]),
    "BAJAJ FINANCE": _eq("BAJFINANCE", sectors=["nbfc"]),
    "HINDUSTAN UNILEVER": _eq("HINDUNILVR", sectors=["fmcg"]),
    "HUL": _eq("HINDUNILVR", sectors=["fmcg"]),
    "BHARTI AIRTEL": _eq("BHARTIARTL", sectors=["telecom"]),
    "AIRTEL": _eq("BHARTIARTL", sectors=["telecom"]),
    "NTPC": _eq("NTPC", sectors=["power"]),
    "ASIAN PAINTS": _eq("ASIANPAINT", sectors=["paints"]),
    "ONGC": _eq("ONGC", sectors=["energy", "oil_gas"]),
    "POWER GRID": _eq("POWERGRID", sectors=["power"]),
    "TITAN": _eq("TITAN", sectors=["retail"]),
    "ITC": _eq("ITC", sectors=["fmcg"]),
    "LARSEN": _eq("LT", sectors=["infra", "construction"]),
    "L&T": _eq("LT", sectors=["infra", "construction"]),
    "HCLTECH": _eq("HCLTECH", sectors=["it"]),
    "HCL TECH": _eq("HCLTECH", sectors=["it"]),
    "MAHINDRA": _eq("M&M", sectors=["auto"]),
    "COAL INDIA": _eq("COALINDIA", sectors=["mining"]),
    "HERO MOTOCORP": _eq("HEROMOTOCO", sectors=["auto"]),
    "HINDALCO": _eq("HINDALCO", sectors=["metals"]),
    "JSW STEEL": _eq("JSWSTEEL", sectors=["metals"]),
    "ULTRATECH": _eq("ULTRACEMCO", sectors=["cement"]),
    "GRASIM": _eq("GRASIM", sectors=["cement"]),
    "BPCL": _eq("BPCL", sectors=["energy", "oil_gas"]),
    "CIPLA": _eq("CIPLA", sectors=["pharma"]),
    "DR REDDY": _eq("DRREDDY", sectors=["pharma"]),
    "EICHER MOTORS": _eq("EICHERMOT", sectors=["auto"]),
    "ADANI ENTERPRISES": _eq("ADANIENT", sectors=["conglomerate"]),
    "ADANI PORTS": _eq("ADANIPORTS", sectors=["infra", "logistics"]),
    "APOLLO HOSPITALS": _eq("APOLLOHOSP", sectors=["healthcare"]),
    "TATA CONSUMER": _eq("TATACONSUM", sectors=["fmcg"]),
    "TRENT": _eq("TRENT", sectors=["retail"]),
    "BAJAJ FINSERV": _eq("BAJAJFINSV", sectors=["nbfc"]),
    "DIVI'S LAB": _eq("DIVISLAB", sectors=["pharma"]),
    "DIVIS LAB": _eq("DIVISLAB", sectors=["pharma"]),
    "SHRIRAM FINANCE": _eq("SHRIRAMFIN", sectors=["nbfc"]),
    # Commodities (MCX) — indices left empty, these aren't NSE index constituents.
    "GOLD": {"symbols": ["GOLD", "GOLDM"], "sectors": ["precious_metals"], "indices": []},
    "SILVER": {"symbols": ["SILVER", "SILVERMIC"], "sectors": ["precious_metals"], "indices": []},
    "CRUDE OIL": {"symbols": ["CRUDEOIL", "CRUDEOILM"], "sectors": ["energy"], "indices": []},
    "NATURAL GAS": {"symbols": ["NATURALGAS"], "sectors": ["energy"], "indices": []},
    "COPPER": {"symbols": ["COPPER"], "sectors": ["base_metals"], "indices": []},
    "ZINC": {"symbols": ["ZINC"], "sectors": ["base_metals"], "indices": []},
    "NICKEL": {"symbols": ["NICKEL"], "sectors": ["base_metals"], "indices": []},
    # Index self-reference — headlines about overall market direction.
    "NIFTY": {"symbols": ["NIFTY"], "sectors": [], "indices": ["NIFTY"]},
    "SENSEX": {"symbols": ["SENSEX"], "sectors": [], "indices": ["SENSEX"]},
    "MIDCAP": {"symbols": ["MIDCPNIFTY"], "sectors": [], "indices": ["MIDCPNIFTY"]},
    "BANKEX": {"symbols": ["BANKEX"], "sectors": ["banking"], "indices": ["BANKEX"]},
    # Macro / thematic — no single symbol, broad index relevance.
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
        # Lexicon fallback — reuses ai/models.py::SentimentAnalyzer's richer
        # (~35/30-word) list instead of maintaining a second, smaller,
        # redundant word set that used to live in this file.
        self._lexicon = SentimentAnalyzer()
        # Optional real scorer (e.g. LocalModelGateway.score_sentiment),
        # injected via set_sentiment_scorer — same pattern as
        # VectorMemoryStore.set_embedder. None (default) means every score
        # comes from the lexicon; a stub/unreachable-runtime gateway
        # correctly degrades to this automatically since it returns None.
        self._sentiment_scorer: Callable[[str, str], float | None] | None = None

    def set_sentiment_scorer(self, scorer: Callable[[str, str], float | None]) -> None:
        self._sentiment_scorer = scorer

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
        sentiment_score = self._sentiment_score(headline, summary)
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

    def _sentiment_score(self, headline: str, summary: str) -> float:
        if self._sentiment_scorer is not None:
            try:
                score = self._sentiment_scorer(headline, summary)
                if score is not None:
                    return max(-1.0, min(1.0, float(score)))
            except Exception as exc:
                note_swallowed("news_intelligence.sentiment_scorer", exc)
        return self._lexicon.analyze(f"{headline} {summary}").score

    def sentiment_for(self, underlying: str) -> float:
        """Average sentiment across currently-active events mapped to
        `underlying` (by symbol or index — same mapping feature_snapshot()
        uses), or 0.0 (honest neutral) if none — this is what
        master_orchestrator.py reads instead of a hardcoded 0.0."""
        symbol = underlying.strip().upper()
        now = datetime.now(timezone.utc)
        scores = [
            event.sentiment_score
            for event in self._events
            if event.expires_at > now
            and any(
                symbol in entity.mapped_symbols or symbol in entity.mapped_indices
                for entity in event.entities
            )
        ]
        return sum(scores) / len(scores) if scores else 0.0

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
