"""Tests for AgentCouncilSupervisor.consult()/_run_many_locked() -- the
batched multi-underlying council path wired in 2026-09-01.

Root cause this fixes: specialists.py's run_batch() (judges many
underlyings in ONE LLM call per specialist, instead of one call per
(specialist, underlying) pair) was fully built and unit-tested
(test_batch_specialists.py) but had zero callers anywhere in the live scan
path -- confirmed live 2026-09-01: even a single, completely uncontended
call to the "fast" model measured 62.9s, so batching demand down (not just
serializing it, which run()'s lock already did) is the real fix for the
87% stub ratio measured that day.
"""
from __future__ import annotations

import re
import threading
import time
import unittest

from trading_platform.agents.schemas import AgentInputContext
from trading_platform.agents.supervisor import AgentCouncilSupervisor


def _ctx(sym: str) -> AgentInputContext:
    return AgentInputContext(trace_id=f"t-{sym}", symbols=[sym], execution_mode="PAPER")


class _CountingGateway:
    """Answers batched (build_batch_prompt) calls with HOLD for every
    instrument mentioned, and single-instrument calls (PM/ExecutionAnalyst)
    with a generic HOLD reply. Thread-safe call counter."""

    fast_model = "fake-fast"
    primary_model = "fake-primary"
    coordinator_model = "fake-coordinator"
    max_tokens = 2048
    rag_retriever = None

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def generate(self, model, system, prompt, **kw):
        with self._lock:
            self.calls += 1
        syms = re.findall(r"--- INSTRUMENT: (\S+) ---", prompt)
        if syms:
            return {"results": [
                {"symbol": s, "action": "HOLD", "confidence": 0.5, "reasoning": "r"} for s in syms
            ]}
        return {"action": "HOLD", "confidence": 0.5, "reasoning": "r"}


class ConsultBatchingTests(unittest.TestCase):
    def test_concurrent_consults_share_one_call_per_specialist(self):
        """The entire point: 4 underlyings admitted around the same instant
        must cost ~9 batched specialist calls total, not 4x9=36 separate ones."""
        gw = _CountingGateway()
        sup = AgentCouncilSupervisor(gw, batch_window_s=0.3, max_batch_size=20)

        results: dict[str, object] = {}

        def worker(sym: str) -> None:
            results[sym] = sup.consult(_ctx(sym))

        threads = [threading.Thread(target=worker, args=(s,)) for s in ("A", "B", "C", "D")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(len(results), 4)
        for sym, decision in results.items():
            self.assertIsNotNone(decision, sym)
        # 9 batched specialist calls, plus per-context PM/ExecutionAnalyst
        # for each non-vetoed underlying (<=4x2=8) -- generously bounded well
        # below what per-underlying fan-out (4x9=36 for specialists alone)
        # would have cost.
        self.assertGreaterEqual(gw.calls, 9)
        self.assertLessEqual(gw.calls, 9 + 4 * 2)

    def test_single_consult_still_completes_after_window(self):
        gw = _CountingGateway()
        sup = AgentCouncilSupervisor(gw, batch_window_s=0.2, max_batch_size=20)
        decision = sup.consult(_ctx("SOLO"))
        self.assertIsNotNone(decision)
        self.assertIn(decision.action, {"PROCEED", "NO_TRADE", "HALT", "REDUCE"})

    def test_full_batch_flushes_immediately_without_waiting_for_window(self):
        gw = _CountingGateway()
        sup = AgentCouncilSupervisor(gw, batch_window_s=5.0, max_batch_size=2)
        results: dict[str, object] = {}

        def worker(sym: str) -> None:
            results[sym] = sup.consult(_ctx(sym))

        t0 = time.monotonic()
        threads = [threading.Thread(target=worker, args=(s,)) for s in ("X", "Y")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        elapsed = time.monotonic() - t0

        self.assertEqual(len(results), 2)
        self.assertLess(
            elapsed, 4.0,
            "hitting max_batch_size should flush immediately, not wait out the 5s window",
        )

    def test_two_separate_batches_do_not_interfere(self):
        """A second wave of consults, arriving after the first batch's
        window already flushed, must form its own independent batch."""
        gw = _CountingGateway()
        sup = AgentCouncilSupervisor(gw, batch_window_s=0.2, max_batch_size=20)
        first = sup.consult(_ctx("WAVE1"))
        second = sup.consult(_ctx("WAVE2"))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        # Two independent single-item batches: 9 + 9 = 18 specialist calls.
        self.assertGreaterEqual(gw.calls, 18)


class RunManyLockedTests(unittest.TestCase):
    def test_one_decision_per_context(self):
        gw = _CountingGateway()
        sup = AgentCouncilSupervisor(gw)
        decisions = sup._run_many_locked([_ctx("A"), _ctx("B"), _ctx("C")])
        self.assertEqual(set(decisions.keys()), {"A", "B", "C"})
        for decision in decisions.values():
            self.assertIn(decision.action, {"PROCEED", "NO_TRADE", "HALT", "REDUCE"})

    def test_one_call_per_specialist_regardless_of_context_count(self):
        gw = _CountingGateway()
        sup = AgentCouncilSupervisor(gw)
        sup._run_many_locked([_ctx(s) for s in "ABCDE"])
        # 9 batchable specialists x 1 chunk (5 <= DEFAULT_BATCH_SIZE) = 9 calls,
        # plus per-context PM/ExecutionAnalyst for each non-vetoed context (<=5x2).
        self.assertGreaterEqual(gw.calls, 9)
        self.assertLessEqual(gw.calls, 9 + 5 * 2)

    def test_empty_contexts_returns_empty(self):
        gw = _CountingGateway()
        sup = AgentCouncilSupervisor(gw)
        self.assertEqual(sup._run_many_locked([]), {})
        self.assertEqual(gw.calls, 0)


if __name__ == "__main__":
    unittest.main()
