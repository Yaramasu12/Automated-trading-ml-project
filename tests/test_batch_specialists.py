"""Batched multi-instrument specialist calls.

A specialist call costs ~6-25s and the council runs once PER UNDERLYING across
58 underlyings per 300s cycle — 580 calls/cycle, ~1.2h, ~15x over budget. That
manifested as SILENT DEGRADATION (timeouts -> canned stub votes), not a slow
cycle. Batching judges N instruments per call; measured 10 instruments in one
~35s call, all 10 with real verdicts.

The property these tests protect is match-by-SYMBOL. Matching by array position
would silently attribute one instrument's verdict to another — a
wrong-but-confident vote, strictly worse than an honest stub.
"""
from __future__ import annotations

import unittest

from trading_platform.agents.schemas import AgentInputContext
from trading_platform.agents.specialists import (
    build_batch_prompt,
    parse_batch_response,
    run_batch,
)


def _ctxs(*syms: str) -> list[AgentInputContext]:
    return [AgentInputContext(trace_id="t", symbols=[s], execution_mode="PAPER") for s in syms]


class _FakeGateway:
    """Stands in for LocalModelGateway; returns a canned reply."""
    fast_model = "fake-fast"
    max_tokens = 2048          # run_batch scales its budget off this

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def generate(self, model, system, prompt, **kw):  # accepts response_schema/max_tokens
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class BatchPromptTests(unittest.TestCase):
    def test_prompt_contains_every_instrument(self):
        p = build_batch_prompt(_ctxs("NIFTY", "BANKNIFTY", "RELIANCE"), "Assess trend.")
        for s in ("NIFTY", "BANKNIFTY", "RELIANCE"):
            self.assertIn(s, p)

    def test_prompt_requests_an_object_not_a_bare_array(self):
        """LocalModelGateway._parse_json rejects non-dict replies, so a bare
        top-level array is silently stubbed. Verified live before this fix:
        10/10 instruments stubbed."""
        p = build_batch_prompt(_ctxs("NIFTY"), "x")
        self.assertIn('"results"', p)


class BatchParseTests(unittest.TestCase):
    def test_matches_by_symbol_not_position(self):
        cs = _ctxs("NIFTY", "BANKNIFTY", "RELIANCE")
        reply = {"results": [
            {"symbol": "RELIANCE", "action": "BUY"},
            {"symbol": "NIFTY", "action": "SELL"},
            {"symbol": "BANKNIFTY", "action": "HOLD"},
        ]}
        m = parse_batch_response(reply, cs)
        self.assertEqual(m["NIFTY"]["action"], "SELL")
        self.assertEqual(m["RELIANCE"]["action"], "BUY")

    def test_hallucinated_symbol_is_rejected(self):
        m = parse_batch_response(
            {"results": [{"symbol": "GHOST", "action": "BUY"}]}, _ctxs("NIFTY"))
        self.assertNotIn("GHOST", m)
        self.assertEqual(m, {})

    def test_missing_instrument_is_simply_absent(self):
        m = parse_batch_response(
            {"results": [{"symbol": "NIFTY", "action": "BUY"}]}, _ctxs("NIFTY", "TCS"))
        self.assertIn("NIFTY", m)
        self.assertNotIn("TCS", m)

    def test_accepts_common_wrapper_keys(self):
        for key in ("results", "instruments", "verdicts", "data", "items"):
            with self.subTest(key=key):
                m = parse_batch_response({key: [{"symbol": "NIFTY", "action": "BUY"}]}, _ctxs("NIFTY"))
                self.assertIn("NIFTY", m)

    def test_garbage_reply_yields_no_matches_rather_than_raising(self):
        for bad in ("not json", None, 42, {"unexpected": "shape"}, []):
            with self.subTest(bad=bad):
                self.assertEqual(parse_batch_response(bad, _ctxs("NIFTY")), {})

    def test_duplicate_symbols_take_the_first(self):
        m = parse_batch_response({"results": [
            {"symbol": "NIFTY", "action": "BUY"},
            {"symbol": "NIFTY", "action": "SELL"},
        ]}, _ctxs("NIFTY"))
        self.assertEqual(m["NIFTY"]["action"], "BUY")


class RunBatchTests(unittest.TestCase):
    def test_one_call_for_many_instruments(self):
        """The entire point: N instruments must cost ONE model call."""
        gw = _FakeGateway({"results": [
            {"symbol": s, "action": "BUY", "confidence": 0.7, "reasoning": "r"}
            for s in ("NIFTY", "BANKNIFTY", "TCS")
        ]})
        votes = run_batch(gw, "m", "A", "task", _ctxs("NIFTY", "BANKNIFTY", "TCS"))
        self.assertEqual(gw.calls, 1)
        self.assertEqual(len(votes), 3)
        self.assertTrue(all(v.action == "BUY" for v in votes))

    def test_returns_one_vote_per_context_in_order(self):
        gw = _FakeGateway({"results": [{"symbol": "TCS", "action": "SELL", "confidence": 0.6, "reasoning": "r"}]})
        votes = run_batch(gw, "m", "A", "task", _ctxs("NIFTY", "TCS"))
        self.assertEqual(len(votes), 2)
        self.assertIn("Stub", votes[0].reasoning)      # NIFTY missing -> stub
        self.assertEqual(votes[1].action, "SELL")      # TCS matched

    def test_only_missing_instruments_are_stubbed(self):
        """A partial reply must not poison the instruments that DID come back."""
        gw = _FakeGateway({"results": [{"symbol": "NIFTY", "action": "BUY", "confidence": 0.8, "reasoning": "r"}]})
        votes = run_batch(gw, "m", "A", "task", _ctxs("NIFTY", "TCS", "ITC"))
        self.assertEqual(votes[0].action, "BUY")
        self.assertNotIn("Stub", votes[0].reasoning)
        self.assertTrue(all("Stub" in v.reasoning for v in votes[1:]))

    def test_gateway_exception_degrades_to_all_stubs_not_a_crash(self):
        gw = _FakeGateway(RuntimeError("llm down"))
        votes = run_batch(gw, "m", "A", "task", _ctxs("NIFTY", "TCS"))
        self.assertEqual(len(votes), 2)
        self.assertTrue(all("Stub" in v.reasoning for v in votes))

    def test_empty_input_makes_no_call(self):
        gw = _FakeGateway({"results": []})
        self.assertEqual(run_batch(gw, "m", "A", "task", []), [])
        self.assertEqual(gw.calls, 0)


if __name__ == "__main__":
    unittest.main()


class ChunkingTests(unittest.TestCase):
    """Batch size 20 measured at 2.03s/instrument vs 3.09s at 10.

    Ceiling worth remembering: batching and concurrency trade off. A single
    batch-20 call succeeds, but 4-8 CONCURRENT batch-20 calls returned 100%
    stubs (80/80, 160/160) while still burning 97-137s — LM Studio serves with
    PARALLEL=4 and cannot hold that many large prompts at once.
    """

    def test_default_batch_size_is_20(self):
        from trading_platform.agents.specialists import DEFAULT_BATCH_SIZE
        self.assertEqual(DEFAULT_BATCH_SIZE, 20)

    def test_chunks_cover_every_context_exactly_once(self):
        from trading_platform.agents.specialists import chunk_contexts
        cs = _ctxs(*[f"S{i}" for i in range(58)])
        chunks = chunk_contexts(cs)
        flat = [c for ch in chunks for c in ch]
        self.assertEqual(len(flat), 58)
        self.assertEqual([c.symbols[0] for c in flat], [c.symbols[0] for c in cs])

    def test_chunk_sizes_respect_the_limit(self):
        from trading_platform.agents.specialists import chunk_contexts
        chunks = chunk_contexts(_ctxs(*[f"S{i}" for i in range(58)]), size=20)
        self.assertEqual([len(c) for c in chunks], [20, 20, 18])

    def test_degenerate_size_falls_back_to_default_and_terminates(self):
        """size=0/None is falsy so it takes the default; negative clamps to 1.
        The property that matters is that neither loops forever."""
        from trading_platform.agents.specialists import chunk_contexts
        self.assertEqual([len(c) for c in chunk_contexts(_ctxs("A", "B"), size=0)], [2])
        self.assertEqual([len(c) for c in chunk_contexts(_ctxs("A", "B"), size=None)], [2])
        self.assertEqual([len(c) for c in chunk_contexts(_ctxs("A", "B"), size=-5)], [1, 1])

    def test_empty_input_yields_no_batches(self):
        from trading_platform.agents.specialists import chunk_contexts
        self.assertEqual(chunk_contexts([]), [])
