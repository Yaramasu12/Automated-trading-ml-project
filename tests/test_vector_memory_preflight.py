"""set_embedder() must not hang startup when the embedding endpoint is
reachable-but-unresponsive (2026-10-13 regression).

LM Studio up with no model loaded accepts the socket instantly but never
answers, so each embed() call blocked up to the 120s read timeout; backfilling
every seed doc hung TradingRuntime.__init__ before uvicorn started. set_embedder
now preflights once under a short wall-clock budget and degrades to Jaccard if
the embedder is slow/dead.
"""
from __future__ import annotations

import time

from trading_platform.agents.vector_memory import VectorMemoryStore


def test_preflight_ok_true_for_working_embedder():
    assert VectorMemoryStore._embedder_preflight_ok(lambda _t: [0.1, 0.2, 0.3]) is True


def test_preflight_ok_true_when_embedder_returns_fast_even_if_none():
    # Responsive = completes fast; a stub that returns None is still responsive.
    assert VectorMemoryStore._embedder_preflight_ok(lambda _t: None) is True


def test_preflight_ok_true_when_embedder_raises_fast():
    # A fast exception (e.g. a strict stub) means responsive — the per-doc
    # backfill handles individual failures; only a HANG must skip.
    def boom(_t):
        raise KeyError("unknown probe input")
    assert VectorMemoryStore._embedder_preflight_ok(boom) is True


def test_preflight_ok_false_and_fast_for_unresponsive_embedder(monkeypatch):
    monkeypatch.setenv("EMBED_PREFLIGHT_TIMEOUT_SECONDS", "2")

    def hang(_t):
        time.sleep(30)
        return [0.1]

    t0 = time.time()
    ok = VectorMemoryStore._embedder_preflight_ok(hang)
    elapsed = time.time() - t0
    assert ok is False
    assert elapsed < 5, f"preflight should bail at the ~2s budget, took {elapsed:.1f}s"


def test_set_embedder_does_not_hang_on_unresponsive_endpoint(monkeypatch):
    monkeypatch.setenv("EMBED_PREFLIGHT_TIMEOUT_SECONDS", "2")
    store = VectorMemoryStore()
    store.seed_defaults()

    def hang(_t):
        time.sleep(30)
        return [0.1]

    t0 = time.time()
    store.set_embedder(hang)  # must return ~2s (preflight), not 30s*N
    elapsed = time.time() - t0
    assert elapsed < 5, f"set_embedder hung ({elapsed:.1f}s) instead of degrading"
    # Degraded to Jaccard: seed docs were NOT backfilled with embeddings.
    assert all(d.embedding is None for d in store._docs.values())


def test_set_embedder_backfills_with_a_working_embedder():
    store = VectorMemoryStore()
    store.seed_defaults()
    store.set_embedder(lambda _t: [0.01, 0.02, 0.03])
    assert any(d.embedding is not None for d in store._docs.values())
