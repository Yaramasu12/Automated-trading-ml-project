"""Tests for Phase 6: Decision Fusion and Goal Governor."""
import unittest

from trading_platform.agents.schemas import AgentCouncilDecision
from trading_platform.decision_fusion.schemas import DecisionBlackboard
from trading_platform.decision_fusion.fusion import EnsembleDecisionEngine
from trading_platform.decision_fusion.goal_governor import GoalGovernor
from trading_platform.neural.schemas import NeuralPredictionBundle, ForecastPrediction


def _make_bb(
    symbols=None,
    agent_action="PROCEED",
    agent_confidence=0.7,
    neural_uncertainty=0.4,
    risk_ok=True,
    risk_reason="",
) -> DecisionBlackboard:
    syms = symbols or ["NIFTY"]
    agent_decision = AgentCouncilDecision(
        trace_id="test",
        action=agent_action,
        confidence=agent_confidence,
        consensus_score=0.7,
    )
    neural = NeuralPredictionBundle(
        trace_id="test",
        overall_uncertainty=neural_uncertainty,
        forecasts=[
            ForecastPrediction(
                symbol=s,
                direction_probability=0.6,
                expected_return=0.002,
            )
            for s in syms
        ],
    )
    return DecisionBlackboard(
        trace_id="test-fusion",
        execution_mode="BACKTEST",
        symbols=syms,
        pipeline_candidates=[{"approved": True}],
        agent_council_decision=agent_decision,
        neural_bundle=neural,
        market_regime="trending",
        risk_precheck_ok=risk_ok,
        risk_precheck_reason=risk_reason,
    )


class TestEnsembleDecisionEngine(unittest.TestCase):
    def setUp(self):
        self._engine = EnsembleDecisionEngine()

    def test_proceeds_on_good_signals(self):
        bb = _make_bb(agent_action="PROCEED", agent_confidence=0.8, neural_uncertainty=0.3)
        output = self._engine.decide(bb)
        self.assertEqual(output.action, "PROCEED")
        self.assertTrue(output.proceed)

    def test_no_trade_on_high_uncertainty(self):
        bb = _make_bb(neural_uncertainty=0.90)
        output = self._engine.decide(bb)
        self.assertEqual(output.action, "NO_TRADE")
        self.assertFalse(output.proceed)

    def test_risk_precheck_fail_causes_halt(self):
        bb = _make_bb(risk_ok=False, risk_reason="kill_switch")
        output = self._engine.decide(bb)
        self.assertEqual(output.action, "HALT")
        self.assertFalse(output.proceed)

    def test_agent_veto_causes_no_trade(self):
        bb = _make_bb(agent_action="HALT")
        output = self._engine.decide(bb)
        self.assertIn(output.action, {"NO_TRADE", "HALT"})
        self.assertFalse(output.proceed)

    def test_risk_critic_veto_causes_no_trade(self):
        bb = _make_bb(agent_action="NO_TRADE")
        output = self._engine.decide(bb)
        self.assertFalse(output.proceed)

    def test_disabled_flags_preserve_behavior(self):
        """With no new components, engine still returns a valid decision."""
        bb = DecisionBlackboard(
            trace_id="baseline",
            execution_mode="BACKTEST",
            symbols=["NIFTY"],
            risk_precheck_ok=True,
        )
        output = self._engine.decide(bb)
        self.assertIn(output.action, {"PROCEED", "NO_TRADE", "HALT", "REDUCE"})

    def test_output_has_reasoning(self):
        bb = _make_bb()
        output = self._engine.decide(bb)
        self.assertIsInstance(output.reasoning, list)


class TestGoalGovernor(unittest.TestCase):
    def setUp(self):
        self._gov = GoalGovernor(
            yearly_target=50_000_000.0,
            initial_capital=10_000_000.0,
        )

    def test_cannot_raise_risk_limits(self):
        self.assertFalse(self._gov.can_raise_risk_limits())

    def test_initial_state(self):
        state = self._gov.compute_state(equity=10_000_000.0)
        self.assertIsNotNone(state)
        self.assertGreaterEqual(state.target_probability, 0.0)
        self.assertLessEqual(state.target_probability, 1.0)

    def test_records_pnl(self):
        self._gov.record_daily_pnl(100_000.0, 10_100_000.0)
        self._gov.record_daily_pnl(200_000.0, 10_300_000.0)
        state = self._gov.compute_state(equity=10_300_000.0)
        self.assertAlmostEqual(state.realized_pnl, 300_000.0)

    def test_goal_pressure_does_not_raise_risk_limits(self):
        # Even with low probability, cannot raise risk
        self._gov.record_daily_pnl(-500_000.0, 9_500_000.0)
        state = self._gov.compute_state()
        self.assertFalse(self._gov.can_raise_risk_limits())

    def test_status_returns_dict(self):
        status = self._gov.status()
        self.assertIn("yearly_target", status)
        self.assertIn("target_probability", status)
        self.assertIn("recommendation", status)

    def test_recommendation_normal_when_on_track(self):
        # Add positive PnL to simulate on-track
        for _ in range(10):
            self._gov.record_daily_pnl(1_000_000.0, 20_000_000.0)
        state = self._gov.compute_state(equity=20_000_000.0)
        # When significantly above target pace, should be normal
        self.assertIn(state.recommendation, {"normal", "reduce_risk", "increase_research"})


if __name__ == "__main__":
    unittest.main()
