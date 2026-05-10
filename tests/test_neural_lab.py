"""Tests for Phase 3: Neural Lab."""
import unittest

from trading_platform.neural.schemas import NeuralPredictionBundle
from trading_platform.neural.forecasting import MovingAverageForecaster, TemporalFusionTransformerForecaster
from trading_platform.neural.volatility import GARCHVolatilityModel, TailRiskModel
from trading_platform.neural.graph_model import RollingCorrelationGraph
from trading_platform.neural.calibration import IdentityCalibrator, IsotonicCalibrator, NO_TRADE_UNCERTAINTY_THRESHOLD
from trading_platform.neural.tensor_features import build_feature_batch
from trading_platform.neural.serving import NeuralPredictionService


def _make_bars(n: int = 60) -> list[dict]:
    price = 100.0
    bars = []
    for i in range(n):
        price *= (1 + (0.001 if i % 2 == 0 else -0.0005))
        bars.append({"open": price, "high": price * 1.002, "low": price * 0.998,
                     "close": price, "volume": 100000 + i})
    return bars


class TestMovingAverageForecaster(unittest.TestCase):
    def test_returns_forecast(self):
        bars = _make_bars(60)
        f = MovingAverageForecaster()
        pred = f.predict("NIFTY", bars)
        self.assertEqual(pred.symbol, "NIFTY")
        self.assertGreater(pred.confidence, 0.0)

    def test_direction_prob_in_range(self):
        bars = _make_bars(40)
        pred = MovingAverageForecaster().predict("X", bars)
        self.assertGreaterEqual(pred.direction_probability, 0.0)
        self.assertLessEqual(pred.direction_probability, 1.0)

    def test_empty_bars(self):
        pred = MovingAverageForecaster().predict("X", [])
        self.assertAlmostEqual(pred.direction_probability, 0.5)


class TestGARCHVolatilityModel(unittest.TestCase):
    def test_returns_prediction(self):
        returns = [0.01, -0.005, 0.008, -0.003, 0.012] * 10
        model = GARCHVolatilityModel()
        pred = model.predict("NIFTY", returns)
        self.assertGreater(pred.predicted_volatility, 0.0)
        self.assertGreaterEqual(pred.tail_risk_score, 0.0)
        self.assertLessEqual(pred.tail_risk_score, 1.0)

    def test_empty_returns(self):
        pred = GARCHVolatilityModel().predict("X", [])
        self.assertIsNotNone(pred)


class TestRollingCorrelationGraph(unittest.TestCase):
    def test_two_symbols(self):
        returns_map = {
            "A": [0.01, -0.005, 0.008] * 10,
            "B": [0.01, -0.005, 0.008] * 10,
        }
        model = RollingCorrelationGraph()
        pred = model.predict(["A", "B"], returns_map)
        self.assertGreater(pred.max_pairwise_correlation, 0.5)

    def test_single_symbol(self):
        pred = RollingCorrelationGraph().predict(["A"], {"A": [0.01, -0.01]})
        self.assertEqual(pred.max_pairwise_correlation, 0.0)


class TestCalibration(unittest.TestCase):
    def test_identity(self):
        cal = IdentityCalibrator()
        self.assertAlmostEqual(cal.calibrate(0.7), 0.7)
        self.assertEqual(cal.calibrate(1.5), 1.0)
        self.assertEqual(cal.calibrate(-0.5), 0.0)

    def test_isotonic_unfitted(self):
        cal = IsotonicCalibrator()
        self.assertAlmostEqual(cal.calibrate(0.6), 0.6)

    def test_no_trade_threshold_defined(self):
        self.assertGreater(NO_TRADE_UNCERTAINTY_THRESHOLD, 0.0)
        self.assertLess(NO_TRADE_UNCERTAINTY_THRESHOLD, 1.0)


class TestNeuralFeatureBatch(unittest.TestCase):
    def test_build_batch(self):
        bars = _make_bars(30)
        batch = build_feature_batch("NIFTY", bars)
        self.assertEqual(batch.symbol, "NIFTY")
        self.assertGreater(len(batch.values), 0)
        self.assertEqual(len(batch.feature_ids), len(batch.values))

    def test_empty_bars(self):
        batch = build_feature_batch("X", [])
        self.assertEqual(len(batch.values), 0)


class TestNeuralPredictionService(unittest.TestCase):
    def test_predict_returns_bundle(self):
        svc = NeuralPredictionService()
        bars_map = {"NIFTY": _make_bars(60)}
        bundle = svc.predict("trace-001", ["NIFTY"], bars_map)
        self.assertIsInstance(bundle, NeuralPredictionBundle)
        self.assertEqual(len(bundle.forecasts), 1)

    def test_empty_symbols(self):
        svc = NeuralPredictionService()
        bundle = svc.predict("trace-002", [], {})
        self.assertEqual(len(bundle.forecasts), 0)
        self.assertAlmostEqual(bundle.overall_uncertainty, 1.0)

    def test_uncertainty_in_range(self):
        svc = NeuralPredictionService()
        bars_map = {"A": _make_bars(40), "B": _make_bars(40)}
        bundle = svc.predict("trace-003", ["A", "B"], bars_map)
        self.assertGreaterEqual(bundle.overall_uncertainty, 0.0)
        self.assertLessEqual(bundle.overall_uncertainty, 1.0)

    def test_lazy_import_no_fail_without_torch(self):
        """TFT forecaster import failure must not crash the service."""
        forecaster = TemporalFusionTransformerForecaster()
        # Either available or not — must not raise
        _ = forecaster.is_available()

    def test_bundle_schema_complete(self):
        svc = NeuralPredictionService()
        bars_map = {"NIFTY": _make_bars(30)}
        bundle = svc.predict("trace-004", ["NIFTY"], bars_map)
        d = bundle.to_dict()
        self.assertIn("trace_id", d)
        self.assertIn("forecasts", d)
        self.assertIn("model_versions", d)
        self.assertIn("overall_uncertainty", d)


if __name__ == "__main__":
    unittest.main()
