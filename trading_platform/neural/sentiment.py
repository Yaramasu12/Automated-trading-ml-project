from __future__ import annotations

"""Sentiment model — wraps existing SentimentAnalyzer with standardized output."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_sentiment_score(symbol: str, sentiment_map: dict[str, float] | None = None) -> float:
    """Return normalized sentiment score for a symbol in [-1, 1].

    Falls back to 0.0 (neutral) if no data available.
    """
    if not sentiment_map:
        return 0.0
    raw = sentiment_map.get(symbol, sentiment_map.get("market", 0.0))
    return max(-1.0, min(1.0, float(raw)))


class SentimentFeatureModel:
    """Wraps an optional SentimentAnalyzer into a standardized scalar output."""

    def __init__(self, analyzer: Any | None = None) -> None:
        self._analyzer = analyzer

    def score(self, symbol: str, news_items: list[str] | None = None) -> float:
        if self._analyzer is None or not news_items:
            return 0.0
        try:
            if hasattr(self._analyzer, "analyze"):
                result = self._analyzer.analyze(symbol, news_items)
                if isinstance(result, (int, float)):
                    return max(-1.0, min(1.0, float(result)))
                if isinstance(result, dict):
                    return max(-1.0, min(1.0, float(result.get("score", 0.0))))
        except Exception as exc:
            logger.debug("SentimentFeatureModel: error for %s: %s", symbol, exc)
        return 0.0
