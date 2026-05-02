from __future__ import annotations

import unittest

from trading_platform.ai.agents import ModelPerformance
from trading_platform.ai.models import ModelRegistry, SentimentAnalyzer, VolatilityForecaster


class AIModelTests(unittest.TestCase):
    def test_volatility_forecaster_returns_interval(self):
        closes = [100, 101, 100.5, 102, 103, 102.2, 104, 105, 103.8, 106]

        forecast = VolatilityForecaster().forecast(closes)

        self.assertEqual(forecast.model_name, "ewma_volatility")
        self.assertGreater(forecast.daily_volatility, 0)
        self.assertLess(forecast.lower_95, forecast.upper_95)

    def test_volatility_interval_evaluation_reports_coverage(self):
        forecaster = VolatilityForecaster()
        closes = [100, 101, 100.5, 102, 103, 102.2, 104, 105, 103.8, 106]
        forecast = forecaster.forecast(closes)

        evaluation = forecaster.evaluate_interval(closes, forecast)

        self.assertGreaterEqual(evaluation.coverage, 0)
        self.assertLessEqual(evaluation.coverage, 1)

    def test_sentiment_baseline_scores_financial_text(self):
        result = SentimentAnalyzer().analyze("Company posts strong profit growth and record gain")

        self.assertEqual(result.label, "POSITIVE")
        self.assertGreater(result.score, 0)
        self.assertGreater(result.confidence, 0.5)

    def test_model_registry_falls_back_to_baseline_when_quality_is_weak(self):
        result = ModelRegistry().select(
            [
                ModelPerformance("lstm_sequence", 0.9, -0.1, 0.05, 0.92, 0.91, 100),
            ]
        )

        self.assertEqual(result.selected_model, "baseline_rules")
        self.assertEqual(result.reason, "no_candidate_beat_baseline_quality_gates")

    def test_model_registry_selects_quality_candidate(self):
        result = ModelRegistry().select(
            [
                ModelPerformance("garch_baseline", 1.3, 0.8, 0.04, 0.94, 0.90, 100),
            ]
        )

        self.assertEqual(result.selected_model, "garch_baseline")


if __name__ == "__main__":
    unittest.main()
