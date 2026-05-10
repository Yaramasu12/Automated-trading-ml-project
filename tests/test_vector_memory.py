"""Tests for trading_platform.agents.vector_memory."""

from __future__ import annotations

import threading

import pytest

from trading_platform.agents.vector_memory import (
    RAGRetriever,
    VectorDocument,
    VectorMemoryStore,
    _jaccard_score,
    _tokenise,
)


# ── Tokeniser ─────────────────────────────────────────────────────────────────

class TestTokenise:
    def test_basic(self):
        tokens = _tokenise("trading momentum strategy")
        assert "trading" in tokens
        assert "momentum" in tokens
        assert "strategy" in tokens

    def test_stop_words_removed(self):
        tokens = _tokenise("this is the strategy")
        assert "this" not in tokens
        assert "the" not in tokens
        assert "strategy" in tokens

    def test_short_tokens_removed(self):
        tokens = _tokenise("a go to")
        assert not tokens  # all are short or stop words

    def test_case_normalised(self):
        tokens = _tokenise("Trend MOMENTUM Breakout")
        assert "trend" in tokens
        assert "momentum" in tokens
        assert "breakout" in tokens

    def test_punctuation_stripped(self):
        tokens = _tokenise("trend-momentum, breakout!")
        assert "momentum" in tokens
        assert "breakout" in tokens

    def test_numbers_included(self):
        tokens = _tokenise("nse 500 stocks")
        assert "nse" in tokens
        assert "500" in tokens

    def test_empty(self):
        assert _tokenise("") == set()


# ── Jaccard score ─────────────────────────────────────────────────────────────

class TestJaccardScore:
    def test_identical(self):
        a = {"trend", "momentum", "nifty"}
        assert _jaccard_score(a, a) == pytest.approx(1.0)

    def test_disjoint(self):
        a = {"trend", "momentum"}
        b = {"risk", "drawdown"}
        assert _jaccard_score(a, b) == 0.0

    def test_partial_overlap(self):
        a = {"trend", "momentum", "nifty"}
        b = {"trend", "momentum", "risk"}
        score = _jaccard_score(a, b)
        # intersection=2, union=4
        assert score == pytest.approx(2 / 4, rel=0.01)

    def test_empty_a(self):
        assert _jaccard_score(set(), {"trend"}) == 0.0

    def test_empty_b(self):
        assert _jaccard_score({"trend"}, set()) == 0.0

    def test_empty_both(self):
        assert _jaccard_score(set(), set()) == 0.0


# ── VectorMemoryStore ─────────────────────────────────────────────────────────

class TestVectorMemoryStore:
    def _make_doc(self, doc_id: str, content: str, category: str = "strategy") -> VectorDocument:
        return VectorDocument(doc_id=doc_id, content=content, category=category)

    def test_add_and_count(self):
        store = VectorMemoryStore()
        store.add(self._make_doc("d1", "trend momentum strategy nifty"))
        store.add(self._make_doc("d2", "mean reversion bollinger band"))
        assert store.count() == 2

    def test_remove(self):
        store = VectorMemoryStore()
        store.add(self._make_doc("d1", "trend strategy"))
        store.remove("d1")
        assert store.count() == 0

    def test_remove_missing_no_error(self):
        store = VectorMemoryStore()
        store.remove("nonexistent")  # should not raise

    def test_get_existing(self):
        store = VectorMemoryStore()
        doc = self._make_doc("d1", "content here")
        store.add(doc)
        assert store.get("d1") is doc

    def test_get_missing_returns_none(self):
        store = VectorMemoryStore()
        assert store.get("missing") is None

    def test_search_returns_relevant(self):
        store = VectorMemoryStore()
        store.add(self._make_doc("d1", "trend momentum moving average nifty banknifty"))
        store.add(self._make_doc("d2", "drawdown risk position limit"))
        results = store.search("trend momentum nifty", top_k=5)
        assert len(results) >= 1
        assert results[0][0].doc_id == "d1"

    def test_search_ordered_by_score(self):
        store = VectorMemoryStore()
        store.add(self._make_doc("d1", "trend momentum nifty strategy"))
        store.add(self._make_doc("d2", "trend nifty"))
        store.add(self._make_doc("d3", "risk drawdown unrelated"))
        results = store.search("trend momentum nifty strategy", top_k=3)
        scores = [r[1] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_search_category_filter(self):
        store = VectorMemoryStore()
        store.add(self._make_doc("d1", "trend momentum nifty", category="strategy"))
        store.add(self._make_doc("d2", "trend momentum risk drawdown", category="risk"))
        results = store.search("trend momentum nifty", top_k=5, category_filter="strategy")
        assert all(r[0].category == "strategy" for r in results)

    def test_search_empty_query_returns_empty(self):
        store = VectorMemoryStore()
        store.add(self._make_doc("d1", "trend momentum"))
        results = store.search("", top_k=5)
        assert results == []

    def test_all_categories(self):
        store = VectorMemoryStore()
        store.add(self._make_doc("d1", "strategy content", category="strategy"))
        store.add(self._make_doc("d2", "risk content", category="risk"))
        cats = store.all_categories()
        assert set(cats) == {"strategy", "risk"}

    def test_count_by_category(self):
        store = VectorMemoryStore()
        store.add(self._make_doc("d1", "s1", category="strategy"))
        store.add(self._make_doc("d2", "s2", category="strategy"))
        store.add(self._make_doc("d3", "r1", category="risk"))
        assert store.count(category="strategy") == 2
        assert store.count(category="risk") == 1
        assert store.count() == 3

    def test_seed_defaults_populates(self):
        store = VectorMemoryStore()
        store.seed_defaults()
        assert store.count() >= 20

    def test_seed_defaults_categories(self):
        store = VectorMemoryStore()
        store.seed_defaults()
        cats = set(store.all_categories())
        assert "strategy" in cats
        assert "model" in cats
        assert "risk" in cats
        assert "compliance" in cats
        assert "postmortem" in cats
        assert "market" in cats

    def test_tags_contribute_to_search(self):
        store = VectorMemoryStore()
        doc = VectorDocument(doc_id="d1", content="general content here",
                             category="strategy", tags=["nifty", "banknifty", "momentum"])
        store.add(doc)
        results = store.search("nifty banknifty momentum", top_k=3)
        assert len(results) >= 1
        assert results[0][0].doc_id == "d1"


class TestVectorMemoryStoreThreadSafety:
    def test_concurrent_add_and_search(self):
        store = VectorMemoryStore()
        errors = []

        def adder(i: int) -> None:
            try:
                doc = VectorDocument(doc_id=f"d{i}", content=f"trend momentum nifty {i}",
                                     category="strategy")
                store.add(doc)
            except Exception as exc:
                errors.append(exc)

        def searcher() -> None:
            try:
                store.search("trend momentum nifty", top_k=5)
            except Exception as exc:
                errors.append(exc)

        threads = []
        for i in range(30):
            threads.append(threading.Thread(target=adder, args=(i,)))
            threads.append(threading.Thread(target=searcher))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


# ── RAGRetriever ──────────────────────────────────────────────────────────────

class TestRAGRetriever:
    def _seeded_store(self) -> VectorMemoryStore:
        store = VectorMemoryStore()
        store.seed_defaults()
        return store

    def test_retrieve_returns_evidence_refs(self):
        store = self._seeded_store()
        rag = RAGRetriever(store)
        refs = rag.retrieve("trend momentum nifty strategy")
        assert len(refs) >= 1
        for ref in refs:
            assert ref.doc_id
            assert ref.source
            assert 0.0 <= ref.relevance <= 1.0
            assert ref.excerpt

    def test_retrieve_ids_returns_strings(self):
        store = self._seeded_store()
        rag = RAGRetriever(store)
        ids = rag.retrieve_ids("trend momentum nifty strategy")
        assert isinstance(ids, list)
        for _id in ids:
            assert isinstance(_id, str)

    def test_retrieve_top_k_respected(self):
        store = self._seeded_store()
        rag = RAGRetriever(store, default_top_k=3)
        refs = rag.retrieve("trend momentum strategy")
        assert len(refs) <= 3

    def test_retrieve_min_score_filters(self):
        store = VectorMemoryStore()
        # Doc with zero overlap to query
        store.add(VectorDocument(doc_id="d1", content="xyz abc qrs", category="strategy"))
        rag = RAGRetriever(store, min_score=0.05)
        refs = rag.retrieve("trend momentum nifty")
        assert refs == []

    def test_build_context_snippet_format(self):
        store = self._seeded_store()
        rag = RAGRetriever(store)
        snippet = rag.build_context_snippet("trend momentum nifty strategy")
        assert snippet.startswith("[Evidence]")
        assert "[" in snippet

    def test_build_context_snippet_empty_on_no_match(self):
        store = VectorMemoryStore()
        rag = RAGRetriever(store)
        snippet = rag.build_context_snippet("trend momentum")
        assert snippet == ""

    def test_store_property(self):
        store = VectorMemoryStore()
        rag = RAGRetriever(store)
        assert rag.store is store

    def test_retrieve_category_filter_via_top_k(self):
        store = self._seeded_store()
        rag = RAGRetriever(store)
        # Ask for risk category only
        refs = rag.retrieve("drawdown halt circuit breaker", category_filter="risk")
        for ref in refs:
            assert ref.source == "risk"

    def test_retrieve_ids_unique(self):
        store = self._seeded_store()
        rag = RAGRetriever(store)
        ids = rag.retrieve_ids("risk drawdown position limit strategy", top_k=10)
        assert len(ids) == len(set(ids))
