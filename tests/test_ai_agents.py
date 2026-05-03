from __future__ import annotations

import unittest

from trading_platform.ai.agents import (
    MarketRegimeAgent,
    ModelPerformance,
    ModelSelectionAgent,
    RetrainingAgent,
    RiskSupervisorAgent,
    StrategySelectionAgent,
)
from trading_platform.ai.features import FeatureSnapshot


class AgentTests(unittest.TestCase):
    def test_regime_and_strategy_selection(self):
        features = FeatureSnapshot(
            symbol="NIFTY",
            close=22500,
            momentum_5=0.02,
            momentum_20=0.08,
            realized_volatility=0.01,
            volume_ratio=1.3,
            trend_strength=8.0,
        )
        regime = MarketRegimeAgent().classify(features)
        strategies = StrategySelectionAgent().choose(regime, "NIFTY")

        self.assertEqual(regime, "TRENDING")
        self.assertIn("futures_trend", strategies)

    def test_model_selection_uses_risk_adjusted_quality(self):
        selected = ModelSelectionAgent().choose(
            [
                ModelPerformance("weak", 0.8, -0.1, 0.05, 0.95, 0.90, 50),
                ModelPerformance("strong", 1.4, 1.2, 0.04, 0.95, 0.91, 50),
            ]
        )

        self.assertEqual(selected, "strong")

    def test_retraining_not_triggered_only_by_target_gap(self):
        healthy = ModelPerformance("healthy", 1.3, 1.1, 0.03, 0.95, 0.90, 50)
        should_retrain, reason = RetrainingAgent().should_retrain(healthy, target_gap_pct=0.5)

        self.assertFalse(should_retrain)
        self.assertEqual(reason, "healthy")

    def test_retraining_triggers_on_iv_coverage_failure(self):
        weak = ModelPerformance("iv_model", 1.2, 0.8, 0.03, 0.80, 0.92, 50)
        should_retrain, reason = RetrainingAgent().should_retrain(weak)

        self.assertTrue(should_retrain)
        self.assertEqual(reason, "iv_coverage_below_threshold")

    def test_risk_supervisor_can_only_reduce_or_halt(self):
        decision = RiskSupervisorAgent().decide(
            drawdown=0.07,
            daily_loss_pct=0.0,
            rejection_rate=0.0,
            stale_market_data=False,
        )

        self.assertEqual(decision.action, "REDUCE")
        self.assertLessEqual(decision.max_allocation_multiplier, 1.0)

    def test_risk_supervisor_halts_on_stale_data(self):
        decision = RiskSupervisorAgent().decide(
            drawdown=0.0,
            daily_loss_pct=0.0,
            rejection_rate=0.0,
            stale_market_data=True,
        )

        self.assertEqual(decision.action, "HALT")
        self.assertEqual(decision.max_allocation_multiplier, 0.0)


if __name__ == "__main__":
    unittest.main()
