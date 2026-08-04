"""RSS ingestion for real financial news — feeds NewsIntelligence.analyze()
with actual headlines instead of the hardcoded 0.0 news_sentiment that
existed because nothing in this codebase ever fetched real news (see
api/ai_capabilities.py's "no_data_source" status and
orchestrator/master_orchestrator.py's _node_market_intelligence comment).

Deliberately advisory-only: this module has no knowledge of
EventRiskGuard and never calls NewsService.news_analyze() (which registers
temporary platform-wide entry blocks) — only NewsIntelligence.analyze()
directly. Continuous RSS-volume ingestion going through the same path
designed for rare, human-curated event submissions would be a materially
different trust assumption; see TradingRuntime._periodic_news_fetch_loop
for where that line is drawn.
"""
from __future__ import annotations

import calendar
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Free, no-API-key Indian financial news RSS feeds. Business Standard was
# also tried (2026-08-04) and dropped — WAF-blocked (403) regardless of
# User-Agent, not a code problem to fix.
FEED_SOURCES: list[tuple[str, str]] = [
    ("Economic Times Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Moneycontrol Business", "https://www.moneycontrol.com/rss/business.xml"),
    ("Moneycontrol Market Reports", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("LiveMint Markets", "https://www.livemint.com/rss/markets"),
]

# Bounds memory for the seen-link dedup set — old entries just get
# re-analyzed (harmless: NewsIntelligence._events is itself a bounded
# maxlen=10_000 deque) rather than tracked forever.
_MAX_SEEN = 4000


class NewsFeedFetcher:
    """Polls FEED_SOURCES, returns only new entries as NewsIntelligence.analyze()-
    ready payloads. `parse_fn` is injectable (defaults to feedparser.parse)
    so tests never make real network calls."""

    def __init__(self, parse_fn: Callable[[str], Any] | None = None) -> None:
        if parse_fn is None:
            import feedparser
            parse_fn = feedparser.parse
        self._parse_fn = parse_fn
        self._seen: deque[tuple[str, str]] = deque(maxlen=_MAX_SEEN)
        self._seen_set: set[tuple[str, str]] = set()

    def _mark_seen(self, key: tuple[str, str]) -> None:
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)

    def fetch_new_items(self) -> list[dict[str, Any]]:
        """Fetch every configured feed. Each feed is independent — one
        unreachable/malformed feed must not stop the others."""
        items: list[dict[str, Any]] = []
        for source_name, url in FEED_SOURCES:
            try:
                items.extend(self._fetch_one(source_name, url))
            except Exception as exc:
                logger.warning("news feed fetch failed for %s (%s): %s", source_name, url, exc)
        return items

    def _fetch_one(self, source_name: str, url: str) -> list[dict[str, Any]]:
        parsed = self._parse_fn(url)
        entries = getattr(parsed, "entries", None) or parsed.get("entries", [])
        out: list[dict[str, Any]] = []
        for entry in entries:
            link = _get(entry, "link")
            title = _get(entry, "title")
            if not link or not title:
                continue
            key = (source_name, link)
            if key in self._seen_set:
                continue
            self._mark_seen(key)
            out.append({
                "headline": title,
                "summary": _get(entry, "summary") or _get(entry, "description") or title,
                "source": source_name,
                "source_url": link,
                "country": "IN",
                "published_at": _published_at(entry),
            })
        return out


def _get(entry: Any, key: str) -> str | None:
    value = entry.get(key) if hasattr(entry, "get") else getattr(entry, key, None)
    return str(value).strip() if value else None


def _published_at(entry: Any) -> datetime:
    struct = entry.get("published_parsed") if hasattr(entry, "get") else getattr(entry, "published_parsed", None)
    if struct:
        try:
            return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)
