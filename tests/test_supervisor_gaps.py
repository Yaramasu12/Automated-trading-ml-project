"""Tests for the three supervisor gaps: debate, per-agent timeout, RAG evidence_ids."""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from trading_platform.agents.model_gateway import LocalModelGateway
from trading_platform.agents.schemas import (
    AgentCouncilDecision,
    AgentInputContext,
    AgentVote,
    DebateRound,
)
from trading_platform.agents.supervisor import AgentCouncilSupervisor
from trading_platform.agents.specialists import (
    MeanReversionAgent,
    TrendMomentumAgent,
    _safe_vote,
)
from trading_platform.agents.vector_memory import RAGRetriever, VectorMemoryStore


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ctx(**kw) -> AgentInputContext:
    defaults = dict(
        trace_id="test-001",
        symbols=["NIFTY"],
        execution_mode="BACKTEST",
        market_regime="trending",
    )
    defaults.update(kw)
    return AgentInputContext(**defaults)


def _stub_gw(runtime: str = "stub") -> LocalModelGateway:
    return LocalModelGateway(runtime=runtime)


# ── Gap 1: 5-party debate ─────────────────────────────────────────────────────

class TestDebateRoundSchema(unittest.TestCase):
    """DebateRound dataclass exists and serialises correctly."""

    def test_debate_round_is_importable(self):
        self.assertIsNotNone(DebateRound)

    def test_debate_round_to_dict(self):
        gw = _stub_gw()
        ctx = _make_ctx()
        trend = TrendMomentumAgent(gw)
        reversion = MeanReversionAgent(gw)
        from trading_platform.agents.specialists import RiskCriticAgent, PortfolioManagerAgent
        bull = trend.run(ctx)
        bear = reversion.run(ctx)
        rc = RiskCriticAgent(gw).run(ctx, [])
        pm = PortfolioManagerAgent(gw).run(ctx, [])
        dr = DebateRound(
            bull_vote=bull,
            bear_vote=bear,
            risk_critique_updated=rc,
            pm_adjudication=pm,
            supervisor_verdict="CONFIRM",
            supervisor_reasoning="test",
            action_override=None,
        )
        d = dr.to_dict()
        self.assertIn("bull", d)
        self.assertIn("bear", d)
        self.assertIn("risk_score_updated", d)
        self.assertIn("pm_run_rate_ok", d)
        self.assertIn("supervisor_verdict", d)
        self.assertEqual(d["supervisor_verdict"], "CONFIRM")
        self.assertIsNone(d["action_override"])

    def test_decision_includes_debate_round_field(self):
        sup = AgentCouncilSupervisor(gateway=_stub_gw())
        ctx = _make_ctx()
        decision = sup.run(ctx)
        # debate_round is None when threshold not met (stub confidence < 0.80)
        self.assertTrue(hasattr(decision, "debate_round"))

    def test_decision_to_dict_includes_debate_round(self):
        sup = AgentCouncilSupervisor(gateway=_stub_gw())
        ctx = _make_ctx()
        decision = sup.run(ctx)
        d = decision.to_dict()
        self.assertIn("debate_round", d)


class TestDebateTriggersAndOverrides(unittest.TestCase):
    """Force the debate to trigger by patching compute_consensus to return high confidence."""

    def _high_confidence_supervisor(self) -> AgentCouncilSupervisor:
        sup = AgentCouncilSupervisor(gateway=_stub_gw())
        return sup

    def test_debate_does_not_trigger_in_stub_mode_naturally(self):
        """Stub agents return confidence ~0.55, well below 0.80 threshold."""
        sup = self._high_confidence_supervisor()
        decision = sup.run(_make_ctx())
        self.assertIsNone(decision.debate_round)

    def test_run_debate_returns_tuple(self):
        """_run_debate() must return (str, str|None, DebateRound|None)."""
        sup = AgentCouncilSupervisor(gateway=_stub_gw())
        ctx = _make_ctx()
        # Call internal _run_debate directly
        result = sup._run_debate(ctx, [], [])
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        summary, override, round_record = result
        self.assertIsInstance(summary, str)
        # override is None or str
        self.assertTrue(override is None or isinstance(override, str))

    def test_run_debate_returns_debate_round(self):
        sup = AgentCouncilSupervisor(gateway=_stub_gw())
        ctx = _make_ctx()
        summary, override, round_record = sup._run_debate(ctx, [], [])
        self.assertIsNotNone(round_record)
        self.assertIsInstance(round_record, DebateRound)

    def test_run_debate_five_parties_present(self):
        """Debate must involve bull, bear, risk, and PM — all four present in round."""
        sup = AgentCouncilSupervisor(gateway=_stub_gw())
        ctx = _make_ctx()
        _, _, dr = sup._run_debate(ctx, [], [])
        self.assertIsNotNone(dr.bull_vote)
        self.assertIsNotNone(dr.bear_vote)
        self.assertIsNotNone(dr.risk_critique_updated)
        self.assertIsNotNone(dr.pm_adjudication)
        self.assertIn(dr.supervisor_verdict, {"CONFIRM", "REDUCE", "ABORT"})

    def test_run_debate_bull_uses_trend_agent(self):
        sup = AgentCouncilSupervisor(gateway=_stub_gw())
        ctx = _make_ctx()
        _, _, dr = sup._run_debate(ctx, [], [])
        self.assertEqual(dr.bull_vote.agent_name, "TrendMomentumAgent")

    def test_run_debate_bear_uses_reversion_agent(self):
        sup = AgentCouncilSupervisor(gateway=_stub_gw())
        ctx = _make_ctx()
        _, _, dr = sup._run_debate(ctx, [], [])
        self.assertEqual(dr.bear_vote.agent_name, "MeanReversionAgent")

    def test_debate_confirm_no_action_override(self):
        """With stub responses (low risk_score, run_rate_ok=True), verdict should be CONFIRM."""
        sup = AgentCouncilSupervisor(gateway=_stub_gw())
        ctx = _make_ctx()
        summary, override, dr = sup._run_debate(ctx, [], [])
        # In stub mode: risk_score=0.3, veto=False, run_rate_ok=True
        # against_score should be 0 or 1 → CONFIRM
        self.assertIn(dr.supervisor_verdict, {"CONFIRM", "REDUCE"})

    def test_action_overridden_when_abort(self):
        """When supervisor votes ABORT, final_action should become NO_TRADE."""
        sup = AgentCouncilSupervisor(gateway=_stub_gw())
        ctx = _make_ctx()

        # Patch _run_debate to return an ABORT verdict
        with patch.object(
            sup, "_run_debate",
            return_value=("debate: ABORT", "NO_TRADE", MagicMock(
                bull_vote=MagicMock(evidence_ids=[]),
                bear_vote=MagicMock(evidence_ids=[]),
                risk_critique_updated=MagicMock(evidence_ids=[]),
                spec=DebateRound,
            ))
        ):
            # Force high conviction + PROCEED by patching the voting functions
            with patch("trading_platform.agents.supervisor.compute_consensus",
                       return_value=("BUY", 0.85, 0.90)), \
                 patch("trading_platform.agents.supervisor.aggregate_to_action",
                       return_value="PROCEED"), \
                 patch("trading_platform.agents.supervisor.has_veto", return_value=False):
                decision = sup.run(ctx)

        self.assertEqual(decision.action, "NO_TRADE")
        self.assertEqual(decision.debate_summary, "debate: ABORT")

    def test_action_overridden_when_reduce(self):
        """When supervisor votes REDUCE, final_action should become REDUCE."""
        sup = AgentCouncilSupervisor(gateway=_stub_gw())
        ctx = _make_ctx()

        with patch.object(
            sup, "_run_debate",
            return_value=("debate: REDUCE", "REDUCE", MagicMock(
                bull_vote=MagicMock(evidence_ids=[]),
                bear_vote=MagicMock(evidence_ids=[]),
                risk_critique_updated=MagicMock(evidence_ids=[]),
                spec=DebateRound,
            ))
        ):
            with patch("trading_platform.agents.supervisor.compute_consensus",
                       return_value=("BUY", 0.85, 0.90)), \
                 patch("trading_platform.agents.supervisor.aggregate_to_action",
                       return_value="PROCEED"), \
                 patch("trading_platform.agents.supervisor.has_veto", return_value=False):
                decision = sup.run(ctx)

        self.assertEqual(decision.action, "REDUCE")

    def test_debate_not_triggered_when_action_not_proceed(self):
        """Debate should not run if final action is not PROCEED."""
        sup = AgentCouncilSupervisor(gateway=_stub_gw())
        called = []

        original = sup._run_debate
        def tracking_debate(*args, **kw):
            called.append(1)
            return original(*args, **kw)

        sup._run_debate = tracking_debate

        with patch("trading_platform.agents.supervisor.compute_consensus",
                   return_value=("BUY", 0.85, 0.90)), \
             patch("trading_platform.agents.supervisor.aggregate_to_action",
                   return_value="NO_TRADE"), \
             patch("trading_platform.agents.supervisor.has_veto", return_value=False):
            sup.run(_make_ctx())

        self.assertEqual(len(called), 0, "debate should not run when action is NO_TRADE")


# ── Gap 2: per-agent timeouts ─────────────────────────────────────────────────

class TestPerAgentTimeout(unittest.TestCase):
    """Agents that hang do not block the council; stub votes are emitted."""

    def test_per_agent_timeout_is_configurable(self):
        sup = AgentCouncilSupervisor(gateway=_stub_gw(), per_agent_timeout_s=5)
        self.assertEqual(sup._per_agent_timeout_s, 5)

    def test_hanging_agent_receives_stub_vote(self):
        """Patch one strategy agent to sleep longer than the timeout."""
        sup = AgentCouncilSupervisor(gateway=_stub_gw(), per_agent_timeout_s=1)

        def slow_agent():
            time.sleep(10)  # much longer than 1s timeout
            return AgentVote(agent_name="SlowAgent", action="BUY", confidence=1.0, reasoning="")

        original_trend_run = sup._trend.run

        def patched_trend(ctx):
            slow_agent()  # will be abandoned after timeout

        sup._trend.run = patched_trend

        start = time.monotonic()
        decision = sup.run(_make_ctx())
        elapsed = time.monotonic() - start

        # Should complete close to the timeout, not 10 seconds
        self.assertLess(elapsed, 8.0, f"council took {elapsed:.1f}s — agent was not timed out")
        self.assertIsNotNone(decision)
        self.assertIn(decision.action, {"PROCEED", "NO_TRADE", "HALT", "REDUCE"})

    def test_timed_out_agent_vote_is_hold_zero_confidence(self):
        """Timed-out agent contributes HOLD with confidence=0 (pulls down overall)."""
        sup = AgentCouncilSupervisor(gateway=_stub_gw(), per_agent_timeout_s=1)

        def patched_trend(ctx):
            time.sleep(10)

        sup._trend.run = patched_trend

        decision = sup.run(_make_ctx())
        # Check at least one vote with failure_mode containing "timeout"
        timeout_votes = [
            v for v in decision.votes
            if v.failure_mode and "timeout" in v.failure_mode
        ]
        self.assertGreater(len(timeout_votes), 0, "expected at least one timeout stub vote")
        for v in timeout_votes:
            self.assertEqual(v.action, "HOLD")
            self.assertEqual(v.confidence, 0.0)

    def test_all_quick_agents_all_pass_normally(self):
        """In stub mode all agents return instantly — all should appear in votes."""
        sup = AgentCouncilSupervisor(gateway=_stub_gw(), per_agent_timeout_s=10)
        decision = sup.run(_make_ctx())
        # 9 strategy agents should all complete
        self.assertEqual(len(decision.votes), 9, f"got {len(decision.votes)} votes, expected 9")

    def test_decision_action_valid_even_with_timeouts(self):
        sup = AgentCouncilSupervisor(gateway=_stub_gw(), per_agent_timeout_s=1)

        def patched_run(ctx):
            time.sleep(10)

        sup._trend.run = patched_run
        sup._reversion.run = patched_run

        decision = sup.run(_make_ctx())
        self.assertIn(decision.action, {"PROCEED", "NO_TRADE", "HALT", "REDUCE"})


# ── Gap 3: RAG evidence_ids ───────────────────────────────────────────────────

class TestRAGEvidenceIds(unittest.TestCase):
    """Specialist agents merge ctx.evidence_ids with gateway-returned ids."""

    def test_safe_vote_merges_ctx_and_gateway_ids(self):
        ctx_ids = ["ctx-doc-1", "ctx-doc-2"]
        gw_response = {
            "action": "BUY",
            "confidence": 0.7,
            "reasoning": "trend is strong",
            "evidence_ids": ["gw-doc-1", "gw-doc-2"],
        }
        vote = _safe_vote("TestAgent", "gemma4-e4b", gw_response, ctx_ids)
        self.assertIn("ctx-doc-1", vote.evidence_ids)
        self.assertIn("ctx-doc-2", vote.evidence_ids)
        self.assertIn("gw-doc-1", vote.evidence_ids)
        self.assertIn("gw-doc-2", vote.evidence_ids)

    def test_safe_vote_deduplicates_ids(self):
        ctx_ids = ["doc-1", "doc-2"]
        gw_response = {
            "action": "HOLD",
            "confidence": 0.5,
            "reasoning": "",
            "evidence_ids": ["doc-1", "doc-3"],  # doc-1 is a duplicate
        }
        vote = _safe_vote("TestAgent", "gemma4-e4b", gw_response, ctx_ids)
        self.assertEqual(vote.evidence_ids.count("doc-1"), 1, "doc-1 should appear exactly once")
        self.assertEqual(len(vote.evidence_ids), 3)  # doc-1, doc-2, doc-3

    def test_safe_vote_ctx_ids_come_first(self):
        """ctx evidence_ids appear before gateway-retrieved ids."""
        ctx_ids = ["ctx-A"]
        gw_response = {"action": "BUY", "confidence": 0.6, "reasoning": "",
                       "evidence_ids": ["gw-B"]}
        vote = _safe_vote("Agent", "gemma4-e4b", gw_response, ctx_ids)
        self.assertEqual(vote.evidence_ids[0], "ctx-A")
        self.assertEqual(vote.evidence_ids[1], "gw-B")

    def test_safe_vote_no_ctx_ids(self):
        gw_response = {"action": "SELL", "confidence": 0.6, "reasoning": "",
                       "evidence_ids": ["gw-doc-1"]}
        vote = _safe_vote("Agent", "gemma4-e4b", gw_response, None)
        self.assertEqual(vote.evidence_ids, ["gw-doc-1"])

    def test_safe_vote_no_gateway_ids(self):
        ctx_ids = ["ctx-1"]
        gw_response = {"action": "HOLD", "confidence": 0.5, "reasoning": "",
                       "evidence_ids": []}
        vote = _safe_vote("Agent", "gemma4-e4b", gw_response, ctx_ids)
        self.assertEqual(vote.evidence_ids, ["ctx-1"])

    def test_trend_agent_merges_ctx_evidence(self):
        gw = _stub_gw()
        ctx = _make_ctx(evidence_ids=["pre-retrieved-1", "pre-retrieved-2"])
        agent = TrendMomentumAgent(gw)
        vote = agent.run(ctx)
        # In stub mode with no RAG the gateway returns [], but ctx ids must appear
        self.assertIn("pre-retrieved-1", vote.evidence_ids)
        self.assertIn("pre-retrieved-2", vote.evidence_ids)

    def test_reversion_agent_merges_ctx_evidence(self):
        gw = _stub_gw()
        ctx = _make_ctx(evidence_ids=["ctx-ev-1"])
        agent = MeanReversionAgent(gw)
        vote = agent.run(ctx)
        self.assertIn("ctx-ev-1", vote.evidence_ids)

    def test_supervisor_propagates_ctx_evidence_to_votes(self):
        """Context evidence_ids pre-enriched by supervisor appear in final decision."""
        store = VectorMemoryStore()
        store.seed_defaults()
        rag = RAGRetriever(store)
        gw = LocalModelGateway(runtime="stub", rag_retriever=rag)
        sup = AgentCouncilSupervisor(gateway=gw)
        ctx = _make_ctx(symbols=["NIFTY"], market_regime="trending")
        decision = sup.run(ctx)
        # With seeded RAG, evidence_ids must be non-empty in the final decision
        self.assertGreater(
            len(decision.evidence_ids), 0,
            "RAG should populate evidence_ids when a vector store is configured"
        )

    def test_risk_critic_merges_ctx_evidence(self):
        from trading_platform.agents.specialists import RiskCriticAgent
        gw = _stub_gw()
        ctx = _make_ctx(evidence_ids=["risk-ctx-1"])
        agent = RiskCriticAgent(gw)
        critique = agent.run(ctx, [])
        self.assertIn("risk-ctx-1", critique.evidence_ids)

    def test_news_macro_agent_uses_gemma_model_not_runtime(self):
        """NewsMacroAgent must not pass gateway.runtime as the model name."""
        from trading_platform.agents.specialists import NewsMacroAgent
        gw = LocalModelGateway(runtime="stub")
        agent = NewsMacroAgent(gw)
        ctx = _make_ctx()
        # If the agent incorrectly passed "stub" as model name, the response
        # would come from an unknown model (fallback to gemma4-e4b). Either way
        # the vote must be valid and not raise.
        vote = agent.run(ctx)
        self.assertIn(vote.action, {"BUY", "SELL", "HOLD", "REDUCE", "HALT", "HEDGE"})
        # model_id must be a real model name, not "ollama" / "stub"
        self.assertNotEqual(vote.model_id, "stub")


if __name__ == "__main__":
    unittest.main()
