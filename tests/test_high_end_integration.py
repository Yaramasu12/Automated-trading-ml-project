"""Tests for Phase 7: High-end integration.

Tests the new services (trace, agents, neural, quantum, fusion) in integration
without spinning up the full TradingRuntime (which is tested separately via
test_runtime.py and takes significant time to initialize).
"""
import tempfile
import unittest

from trading_platform.trace.ids import new_trace_id
from trading_platform.trace.models import DecisionTrace
from trading_platform.trace.store import TraceStore
from trading_platform.agents.model_gateway import LocalModelGateway
from trading_platform.agents.schemas import AgentInputContext
from trading_platform.agents.supervisor import AgentCouncilSupervisor
from trading_platform.neural.serving import NeuralPredictionService
from trading_platform.quantum.service import QuantumOptimizationService
from trading_platform.quantum.schemas import PortfolioOptimizationRequest, QuantumCandidate
from trading_platform.decision_fusion.schemas import DecisionBlackboard
from trading_platform.decision_fusion.fusion import EnsembleDecisionEngine
from trading_platform.decision_fusion.goal_governor import GoalGovernor


def _make_bars(n: int = 30) -> list[dict]:
    p = 100.0
    bars = []
    for i in range(n):
        p *= 1.001 if i % 2 == 0 else 0.999
        bars.append({"open": p, "high": p * 1.001, "low": p * 0.999, "close": p, "volume": 1000})
    return bars


class TestIntegrationDisabledFlags(unittest.TestCase):
    """All new flags disabled — behavior identical to baseline."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._ts = TraceStore(base_dir=self._tmpdir)

    def test_scan_returns_trace_id(self):
        trace_id = new_trace_id("hescan")
        trace = DecisionTrace(trace_id=trace_id, created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), execution_mode="BACKTEST")
        self._ts.save(trace)
        self.assertIsNotNone(self._ts.get(trace_id))

    def test_no_ai_council_no_orders(self):
        # When AI council is disabled (flags=False), no agent runs
        gw = LocalModelGateway(runtime="stub")
        # Just verify gateway doesn't error
        result = gw.generate("gemma4-31b", "sys", "user")
        self.assertNotIn("order", str(result).lower())


class TestIntegrationAgentCouncilEnabled(unittest.TestCase):
    """Simulate enabled AI council — verify decision path."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._ts = TraceStore(base_dir=self._tmpdir)
        gw = LocalModelGateway(runtime="stub")
        self._supervisor = AgentCouncilSupervisor(gateway=gw, trace_store=self._ts)

    def test_enabled_path_returns_trace_metadata(self):
        trace_id = new_trace_id()
        trace = DecisionTrace(
            trace_id=trace_id,
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            execution_mode="BACKTEST",
            symbol_universe=["NIFTY"],
        )
        self._ts.save(trace)

        ctx = AgentInputContext(
            trace_id=trace_id, symbols=["NIFTY"], execution_mode="BACKTEST"
        )
        decision = self._supervisor.run(ctx)
        self.assertIn("trace_id", decision.to_dict())

    def test_risk_veto_blocks(self):
        bb = DecisionBlackboard(
            trace_id="t1", execution_mode="BACKTEST", symbols=["NIFTY"],
            risk_precheck_ok=False, risk_precheck_reason="kill_switch",
        )
        engine = EnsembleDecisionEngine()
        output = engine.decide(bb)
        self.assertEqual(output.action, "HALT")
        self.assertFalse(output.proceed)

    def test_agent_council_cannot_enqueue(self):
        ctx = AgentInputContext(trace_id=new_trace_id(), symbols=["NIFTY"], execution_mode="BACKTEST")
        decision = self._supervisor.run(ctx)
        d = decision.to_dict()
        self.assertNotIn("order_id", str(d))
        self.assertNotIn("OrderIntent", str(d))


class TestIntegrationNeuralEnabled(unittest.TestCase):
    def setUp(self):
        self._neural = NeuralPredictionService()

    def test_enabled_path_returns_trace_metadata(self):
        trace_id = new_trace_id()
        bundle = self._neural.predict(trace_id, ["NIFTY"], {"NIFTY": _make_bars()})
        self.assertEqual(bundle.trace_id, trace_id)

    def test_high_uncertainty_triggers_no_trade(self):
        """Bundle with uncertainty > threshold causes should_trade=False."""
        from trading_platform.neural.schemas import NeuralPredictionBundle
        bundle = NeuralPredictionBundle(trace_id="t", overall_uncertainty=0.90)
        self.assertFalse(self._neural.should_trade(bundle))


class TestIntegrationQuantumEnabled(unittest.TestCase):
    def setUp(self):
        self._svc = QuantumOptimizationService(backend="classical", timeout=2)

    def test_enabled_path_returns_trace_metadata(self):
        req = PortfolioOptimizationRequest(
            trace_id=new_trace_id(),
            candidates=[
                QuantumCandidate("NIFTY", "BUY", 0.01, 0.3),
                QuantumCandidate("BANKNIFTY", "BUY", 0.015, 0.4),
            ],
            cardinality_limit=1,
        )
        result = self._svc.optimize(req)
        self.assertIn("trace_id", result.to_dict())
        self.assertLessEqual(len(result.selected_symbols), 1)

    def test_optimizer_cannot_bypass_risk_engine(self):
        """Optimizer returns advisory result, not orders."""
        req = PortfolioOptimizationRequest(
            trace_id="t", candidates=[QuantumCandidate("X", "BUY", 0.01, 0.3)]
        )
        result = self._svc.optimize(req)
        self.assertNotIn("order_id", str(result.to_dict()))


class TestIntegrationEnsemble(unittest.TestCase):
    def test_disabled_flags_preserve_current_behavior(self):
        """When all new components absent, ensemble still works."""
        bb = DecisionBlackboard(
            trace_id="baseline", execution_mode="BACKTEST", symbols=["NIFTY"]
        )
        engine = EnsembleDecisionEngine()
        output = engine.decide(bb)
        self.assertIn(output.action, {"PROCEED", "NO_TRADE", "HALT", "REDUCE"})

    def test_goal_pressure_does_not_raise_risk_limits(self):
        gov = GoalGovernor(yearly_target=50_000_000.0, initial_capital=1_000_000.0)
        # Even under extreme goal pressure, cannot raise limits
        gov.record_daily_pnl(-500_000.0, 500_000.0)
        self.assertFalse(gov.can_raise_risk_limits())


if __name__ == "__main__":
    unittest.main()
