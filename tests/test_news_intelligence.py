"""Tests for the 2026-08-04 news_sentiment fix: real RSS ingestion + local-LLM
sentiment scoring, closing the gap where master_orchestrator.py hardcoded
news_sentiment = 0.0 every cycle (see api/ai_capabilities.py's former
"no_data_source" status)."""
from __future__ import annotations

import unittest

from trading_platform.agent.trading_agent import EQUITY_UNDERLYINGS
from trading_platform.news.intelligence import NewsIntelligence, _ENTITY_MAP


def _payload(headline="Company beats earnings estimates", underlying_mention="RELIANCE"):
    return {
        "headline": f"{underlying_mention} {headline}",
        "summary": "details",
        "source": "test",
        "source_url": "https://example.com/1",
    }


class SentimentForTests(unittest.TestCase):
    def test_no_matching_events_returns_neutral(self):
        ni = NewsIntelligence()
        self.assertEqual(ni.sentiment_for("RELIANCE"), 0.0)

    def test_matching_event_contributes_to_average(self):
        ni = NewsIntelligence()
        ni.analyze(_payload(headline="beat profit record", underlying_mention="RELIANCE"))
        score = ni.sentiment_for("RELIANCE")
        self.assertGreater(score, 0.0)

    def test_unrelated_symbol_stays_neutral(self):
        ni = NewsIntelligence()
        ni.analyze(_payload(headline="beat profit record", underlying_mention="RELIANCE"))
        self.assertEqual(ni.sentiment_for("WIPRO"), 0.0)

    def test_expired_event_excluded(self):
        ni = NewsIntelligence()
        payload = _payload(headline="beat profit record", underlying_mention="RELIANCE")
        payload["risk_ttl_hours"] = 0.0
        ni.analyze(payload)
        # expires_at = received_at + 0h -> already expired by the time we check
        self.assertEqual(ni.sentiment_for("RELIANCE"), 0.0)


class SentimentScorerFallbackTests(unittest.TestCase):
    """Mirrors test_vector_memory.py's TestEmbeddingSearch fallback pattern:
    an injected scorer is used when it returns a value, the lexicon fallback
    otherwise — never an error propagated to the caller."""

    def test_no_scorer_uses_lexicon(self):
        ni = NewsIntelligence()
        ni.analyze(_payload(headline="beat record profit", underlying_mention="RELIANCE"))
        events = ni.recent_events(1)
        self.assertGreater(events[0]["sentiment_score"], 0.0)

    def test_injected_scorer_used_when_it_returns_a_value(self):
        ni = NewsIntelligence()
        ni.set_sentiment_scorer(lambda headline, summary: -0.8)
        ni.analyze(_payload(headline="beat record profit", underlying_mention="RELIANCE"))
        events = ni.recent_events(1)
        # Lexicon would score this positive; the injected scorer's -0.8 wins.
        self.assertEqual(events[0]["sentiment_score"], -0.8)

    def test_scorer_returning_none_falls_back_to_lexicon(self):
        ni = NewsIntelligence()
        ni.set_sentiment_scorer(lambda headline, summary: None)
        ni.analyze(_payload(headline="beat record profit", underlying_mention="RELIANCE"))
        events = ni.recent_events(1)
        self.assertGreater(events[0]["sentiment_score"], 0.0)

    def test_scorer_raising_falls_back_to_lexicon_without_error(self):
        def boom(headline, summary):
            raise RuntimeError("model unreachable")
        ni = NewsIntelligence()
        ni.set_sentiment_scorer(boom)
        ni.analyze(_payload(headline="beat record profit", underlying_mention="RELIANCE"))  # must not raise
        events = ni.recent_events(1)
        self.assertGreater(events[0]["sentiment_score"], 0.0)


class EntityMapCoverageTests(unittest.TestCase):
    def test_every_scanned_equity_symbol_is_reachable(self):
        # sentiment_for() matches on mapped_symbols OR mapped_indices, so an
        # index like BANKNIFTY is "covered" via any entity that tags it as
        # an index (e.g. the "BANK"/"RBI" macro entries), not necessarily as
        # its own symbols entry.
        reachable = {
            symbol
            for mapping in _ENTITY_MAP.values()
            for symbol in (*mapping["symbols"], *mapping["indices"])
        }
        missing = [s for s in EQUITY_UNDERLYINGS if s not in reachable]
        self.assertEqual(missing, [], f"symbols unreachable via any _ENTITY_MAP entry: {missing}")


if __name__ == "__main__":
    unittest.main()
