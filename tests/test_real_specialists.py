"""The RiskCritic and OptionsVolatility specialists must compute REAL assessments
from actual market/portfolio numbers (GARCH vol, tail risk, drawdown) — not call
an LLM. Deterministic and auditable."""
from __future__ import annotations

import unittest

from trading_platform.agents.schemas import AgentInputContext
from trading_platform.agents.specialists import OptionsVolatilityAgent, RiskCriticAgent


def _ctx(tail_risk=0.0, pred_vol=0.0, drawdown=0.0):
    return AgentInputContext(
        trace_id="t", symbols=["NIFTY"], execution_mode="PAPER",
        features={"tail_risk_score": tail_risk, "predicted_volatility": pred_vol},
        portfolio_state={"drawdown": drawdown},
    )


class RealRiskCriticTests(unittest.TestCase):
    def setUp(self):
        self.agent = RiskCriticAgent(gateway=None)  # gateway unused now

    def test_proceeds_when_calm(self):
        c = self.agent.run(_ctx(tail_risk=0.1, pred_vol=0.10, drawdown=0.0), [])
        self.assertEqual(c.recommended_action, "PROCEED")
        self.assertFalse(c.veto)
        self.assertEqual(c.model_id, "risk_critic_rules_v1")

    def test_halts_on_severe_drawdown(self):
        c = self.agent.run(_ctx(drawdown=0.10), [])
        self.assertEqual(c.recommended_action, "HALT")
        self.assertTrue(c.veto)

    def test_reduces_on_elevated_risk(self):
        c = self.agent.run(_ctx(tail_risk=0.9, pred_vol=0.35, drawdown=0.02), [])
        self.assertIn(c.recommended_action, ("REDUCE", "HALT"))
        self.assertGreater(c.risk_score, 0.5)
        self.assertTrue(any("tail-risk" in s for s in c.concerns))


class RealOptionsVolTests(unittest.TestCase):
    def setUp(self):
        self.agent = OptionsVolatilityAgent(gateway=None)

    def test_no_directional_call_ever(self):
        # Honest: no directional edge -> always HOLD, whatever the vol regime.
        for tr, pv in [(0.1, 0.10), (0.7, 0.35), (0.3, 0.20)]:
            v = self.agent.run(_ctx(tail_risk=tr, pred_vol=pv))
            self.assertEqual(v.action, "HOLD")
            self.assertEqual(v.model_id, "options_vol_garch_v1")

    def test_confidence_reflects_vol_regime(self):
        elevated = self.agent.run(_ctx(tail_risk=0.8, pred_vol=0.35))
        normal = self.agent.run(_ctx(tail_risk=0.2, pred_vol=0.18))
        self.assertIn("elevated", elevated.reasoning)
        self.assertGreater(elevated.confidence, normal.confidence)


if __name__ == "__main__":
    unittest.main()
