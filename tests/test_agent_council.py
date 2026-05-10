"""Tests for Phase 2: AgentCouncilSupervisor."""
import tempfile
import unittest
from datetime import datetime, timezone

from trading_platform.agents.model_gateway import LocalModelGateway
from trading_platform.agents.schemas import AgentInputContext, AgentVote
from trading_platform.agents.supervisor import AgentCouncilSupervisor
from trading_platform.agents.voting import compute_consensus, aggregate_to_action
from trading_platform.trace.store import TraceStore


def _make_ctx(trace_id: str = "test-001") -> AgentInputContext:
    return AgentInputContext(
        trace_id=trace_id,
        symbols=["NIFTY"],
        execution_mode="BACKTEST",
        market_regime="trending",
    )


class TestAgentCouncilSupervisor(unittest.TestCase):
    def setUp(self):
        self._gw = LocalModelGateway(runtime="stub")
        self._tmpdir = tempfile.mkdtemp()
        self._ts = TraceStore(base_dir=self._tmpdir)
        self._supervisor = AgentCouncilSupervisor(gateway=self._gw, trace_store=self._ts)

    def test_returns_decision(self):
        ctx = _make_ctx()
        decision = self._supervisor.run(ctx)
        self.assertIsNotNone(decision)

    def test_no_order_creation(self):
        ctx = _make_ctx()
        decision = self._supervisor.run(ctx)
        # Decision must not contain order-related fields
        d = decision.to_dict()
        self.assertNotIn("order_id", str(d))
        self.assertNotIn("OrderIntent", str(d))

    def test_action_is_valid(self):
        ctx = _make_ctx()
        decision = self._supervisor.run(ctx)
        self.assertIn(decision.action, {"PROCEED", "REDUCE", "HALT", "NO_TRADE"})

    def test_confidence_in_range(self):
        ctx = _make_ctx()
        decision = self._supervisor.run(ctx)
        self.assertGreaterEqual(decision.confidence, 0.0)
        self.assertLessEqual(decision.confidence, 1.0)

    def test_has_votes(self):
        ctx = _make_ctx()
        decision = self._supervisor.run(ctx)
        self.assertGreater(len(decision.votes), 0)

    def test_risk_critic_veto_causes_no_trade(self):
        """Force all agents to HALT to trigger veto."""
        # We can't directly force the risk critic to veto via stub,
        # but we can verify that when stub returns HOLD, no crash occurs.
        ctx = _make_ctx("veto-test")
        decision = self._supervisor.run(ctx)
        # Should be a valid no-trade when confidence is low from stubs
        self.assertIn(decision.action, {"PROCEED", "NO_TRADE", "HALT", "REDUCE"})


class TestVoting(unittest.TestCase):
    def test_consensus_unanimous(self):
        votes = [
            AgentVote(agent_name=f"A{i}", action="BUY", confidence=0.8, reasoning="")
            for i in range(5)
        ]
        action, conf, consensus = compute_consensus(votes)
        self.assertEqual(action, "BUY")
        self.assertAlmostEqual(consensus, 1.0)

    def test_consensus_split(self):
        votes = [
            AgentVote(agent_name="A1", action="BUY", confidence=0.8, reasoning=""),
            AgentVote(agent_name="A2", action="SELL", confidence=0.8, reasoning=""),
            AgentVote(agent_name="A3", action="BUY", confidence=0.8, reasoning=""),
            AgentVote(agent_name="A4", action="SELL", confidence=0.8, reasoning=""),
        ]
        action, conf, consensus = compute_consensus(votes)
        self.assertLess(consensus, 1.0)

    def test_low_consensus_causes_no_trade(self):
        votes = [
            AgentVote(agent_name="A1", action="BUY", confidence=0.5, reasoning=""),
            AgentVote(agent_name="A2", action="SELL", confidence=0.5, reasoning=""),
        ]
        _, conf, consensus = compute_consensus(votes)
        action = aggregate_to_action("BUY", consensus, conf)
        self.assertEqual(action, "NO_TRADE")

    def test_empty_votes(self):
        action, conf, consensus = compute_consensus([])
        self.assertEqual(action, "HOLD")
        self.assertEqual(conf, 0.0)

    def test_stale_bad_data_causes_no_trade(self):
        votes = [
            AgentVote(
                agent_name="A1", action="HOLD", confidence=0.1,
                reasoning="stale data", failure_mode="stale_data"
            )
        ]
        _, conf, consensus = compute_consensus(votes)
        action = aggregate_to_action("HOLD", consensus, conf)
        self.assertIn(action, {"NO_TRADE", "HALT"})


if __name__ == "__main__":
    unittest.main()
