"""Tests for trading_platform/news/feed.py's RSS ingestion — no real network
calls; parse_fn is injected with fake feedparser-shaped responses."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from trading_platform.news.feed import NewsFeedFetcher, FEED_SOURCES


def _entry(title="Reliance beats estimates", link="https://example.com/a", summary="details",
           published_parsed=(2026, 8, 4, 6, 0, 0, 1, 216, 0)):
    return SimpleNamespace(
        get=lambda key, default=None: {
            "title": title, "link": link, "summary": summary, "published_parsed": published_parsed,
        }.get(key, default),
    )


def _feed(entries):
    return SimpleNamespace(entries=entries)


class NewsFeedFetcherTests(unittest.TestCase):
    def test_parses_entries_into_analyze_ready_payload(self):
        fetcher = NewsFeedFetcher(parse_fn=lambda url: _feed([_entry()]))
        items = fetcher.fetch_new_items()
        self.assertEqual(len(items), len(FEED_SOURCES))  # same entry from every configured source
        item = items[0]
        self.assertEqual(item["headline"], "Reliance beats estimates")
        self.assertEqual(item["summary"], "details")
        self.assertEqual(item["source_url"], "https://example.com/a")
        self.assertEqual(item["country"], "IN")
        self.assertIn("source", item)
        self.assertIn("published_at", item)

    def test_dedup_skips_already_seen_link_on_next_fetch(self):
        fetcher = NewsFeedFetcher(parse_fn=lambda url: _feed([_entry()]))
        first = fetcher.fetch_new_items()
        second = fetcher.fetch_new_items()
        self.assertEqual(len(first), len(FEED_SOURCES))
        self.assertEqual(second, [])

    def test_entry_missing_link_or_title_is_skipped(self):
        fetcher = NewsFeedFetcher(parse_fn=lambda url: _feed([_entry(title=""), _entry(link="")]))
        items = fetcher.fetch_new_items()
        self.assertEqual(items, [])

    def test_one_broken_feed_does_not_stop_the_others(self):
        calls = {"n": 0}

        def flaky_parse(url):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("unreachable")
            return _feed([_entry()])

        fetcher = NewsFeedFetcher(parse_fn=flaky_parse)
        items = fetcher.fetch_new_items()
        # First source failed, the rest still returned their entry.
        self.assertEqual(len(items), len(FEED_SOURCES) - 1)

    def test_missing_published_parsed_falls_back_to_now(self):
        fetcher = NewsFeedFetcher(parse_fn=lambda url: _feed([_entry(published_parsed=None)]))
        items = fetcher.fetch_new_items()
        self.assertTrue(len(items) > 0)
        self.assertIsNotNone(items[0]["published_at"])


if __name__ == "__main__":
    unittest.main()
