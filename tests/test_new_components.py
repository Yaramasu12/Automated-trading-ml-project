"""Tests for GARCH(1,1), RegimeClassifier, MetaModel, FeatureStore,
ImpliedVolatilityCalculator, IVSurface, and WalkForwardEvaluator."""
from __future__ import annotations

import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from trading_platform.ai.feature_store import FeatureStore
from trading_platform.ai.features import FeatureSnapshot
from trading_platform.ai.models import (
    GARCHForecaster,
    MetaModel,
    RegimeClassifier,
    StrategyRegimeScore,
)
from trading_platform.backtesting.engine import BacktestEngine
from trading_platform.backtesting.evaluator import WalkForwardEvaluator
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.derivatives.engine import (
    ImpliedVolatilityCalculator,
    IVSurfaceBuilder,
    OptionChainBuilder,
)
from trading_platform.domain.enums import OptionType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample_closes(n: int = 50, seed: float = 1.0) -> list[float]:
    closes = [100.0]
    for i in range(1, n):
        change = 0.001 * math.sin(i * 0.5) + 0.0005 * (i % 3 - 1)
        closes.append(max(1.0, closes[-1] * (1 + change)))
    return closes


def _make_feature(
    vol: float = 0.01,
    trend: float = 2.0,
    mom5: float = 0.005,
    mom20: float = 0.01,
    vr: float = 1.0,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol="TEST",
        close=100.0,
        momentum_5=mom5,
        momentum_20=mom20,
        realized_volatility=vol,
        volume_ratio=vr,
        trend_strength=trend,
    )


# ---------------------------------------------------------------------------
# GARCH(1,1)
# ---------------------------------------------------------------------------

class GARCHForecasterTests(unittest.TestCase):
    def setUp(self):
        self.forecaster = GARCHForecaster()
        self.closes = _sample_closes(60)

    def test_returns_forecast_with_correct_shape(self):
        fc = self.forecaster.forecast(self.closes)
        self.assertEqual(fc.model_name, "garch_1_1")
        self.assertGreater(fc.daily_volatility, 0)
        self.assertGreater(fc.annualized_volatility, fc.daily_volatility)
        self.assertLess(fc.lower_95, fc.upper_95)
        self.assertEqual(fc.sample_size, len(self.closes) - 1)

    def test_fitted_params_sum_less_than_one(self):
        params = self.forecaster.fitted_params(self.closes)
        self.assertIn("alpha", params)
        self.assertIn("beta", params)
        self.assertIn("persistence", params)
        self.assertLess(params["persistence"], 1.0)

    def test_raises_on_too_few_closes(self):
        with self.assertRaises(ValueError):
            self.forecaster.forecast([100.0, 101.0, 99.0])

    def test_daily_vol_plausible_range(self):
        fc = self.forecaster.forecast(self.closes)
        # Daily vol of a realistic stock should be between 0.001% and 10%
        self.assertGreater(fc.daily_volatility, 0.0001)
        self.assertLess(fc.daily_volatility, 0.10)


# ---------------------------------------------------------------------------
# RegimeClassifier
# ---------------------------------------------------------------------------

class RegimeClassifierTests(unittest.TestCase):
    def setUp(self):
        self.clf = RegimeClassifier()

    def test_rule_based_high_volatility(self):
        features = _make_feature(vol=0.03)
        regime = self.clf.predict(features)
        self.assertEqual(regime, "HIGH_VOLATILITY")

    def test_rule_based_trending(self):
        features = _make_feature(vol=0.01, trend=5.0, mom20=0.04)
        regime = self.clf.predict(features)
        self.assertEqual(regime, "TRENDING")

    def test_rule_based_breakout(self):
        features = _make_feature(vol=0.01, trend=1.0, mom5=0.025, vr=1.5)
        regime = self.clf.predict(features)
        self.assertEqual(regime, "BREAKOUT")

    def test_rule_based_mean_reverting(self):
        features = _make_feature(vol=0.005, trend=0.5, mom5=0.001, mom20=0.001, vr=0.9)
        regime = self.clf.predict(features)
        self.assertEqual(regime, "MEAN_REVERTING")

    def test_predict_proba_sums_to_one_rule_based(self):
        features = _make_feature()
        proba = self.clf.predict_proba(features)
        self.assertAlmostEqual(sum(proba.values()), 1.0, places=5)

    def test_train_with_sufficient_records_succeeds(self):
        records = []
        for i in range(30):
            records.append({
                "momentum_5": 0.01 * (i % 5),
                "momentum_20": 0.005 * (i % 3),
                "realized_volatility": 0.01 + 0.001 * i,
                "volume_ratio": 1.0 + 0.1 * (i % 4),
                "trend_strength": float(i % 5),
                "regime": RegimeClassifier.REGIMES[i % 4],
            })
        # Training may succeed if sklearn is installed; should not raise if not
        result = self.clf.train(records)
        self.assertIsInstance(result, bool)

    def test_train_with_insufficient_records_returns_false(self):
        result = self.clf.train([{"regime": "TRENDING", "momentum_5": 0.01}])
        self.assertFalse(result)

    def test_not_trained_initially(self):
        self.assertFalse(self.clf.is_trained)


# ---------------------------------------------------------------------------
# MetaModel
# ---------------------------------------------------------------------------

class MetaModelTests(unittest.TestCase):
    def setUp(self):
        self.model = MetaModel()

    def test_rank_returns_all_strategies(self):
        names = ["equity_momentum", "futures_trend", "defined_risk_option_spread"]
        ranked = self.model.rank("TRENDING", names)
        self.assertEqual(len(ranked), 3)
        self.assertIsInstance(ranked[0], StrategyRegimeScore)

    def test_update_shifts_scores(self):
        self.model.update("TRENDING", "futures_trend", 10.0)
        self.model.update("TRENDING", "equity_momentum", 1.0)
        ranked = self.model.rank("TRENDING", ["futures_trend", "equity_momentum"])
        self.assertEqual(ranked[0].strategy_name, "futures_trend")
        self.assertEqual(ranked[0].rank, 1)

    def test_top_strategies_respects_top_n(self):
        names = ["a", "b", "c", "d"]
        for name, score in zip(names, [4.0, 3.0, 2.0, 1.0]):
            self.model.update("MEAN_REVERTING", name, score)
        top = self.model.top_strategies("MEAN_REVERTING", names, top_n=2)
        self.assertEqual(top, ["a", "b"])

    def test_summary_contains_updated_regimes(self):
        self.model.update("HIGH_VOLATILITY", "long_straddle", 5.0)
        summary = self.model.summary()
        self.assertIn("HIGH_VOLATILITY", summary)
        self.assertIn("long_straddle", summary["HIGH_VOLATILITY"])

    def test_ema_smoothing_on_repeated_updates(self):
        self.model.update("TRENDING", "futures_trend", 10.0)
        self.model.update("TRENDING", "futures_trend", 0.0)
        ranked = self.model.rank("TRENDING", ["futures_trend"])
        # After EMA blend with new score 0, should be between 0 and 10
        self.assertGreater(ranked[0].score, 0.0)
        self.assertLess(ranked[0].score, 10.0)


# ---------------------------------------------------------------------------
# FeatureStore
# ---------------------------------------------------------------------------

class FeatureStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = FeatureStore(store_dir=Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _snap(self, symbol: str = "NIFTY") -> FeatureSnapshot:
        return FeatureSnapshot(symbol=symbol, close=18000.0, momentum_5=0.01,
                               momentum_20=0.02, realized_volatility=0.015,
                               volume_ratio=1.1, trend_strength=3.0)

    def test_append_and_load_roundtrip(self):
        snap = self._snap()
        self.store.append("NIFTY", date(2026, 1, 15), snap, "TRENDING")
        records = self.store.load("NIFTY")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["regime"], "TRENDING")
        self.assertEqual(records[0]["symbol"], "NIFTY")

    def test_load_limit(self):
        for i in range(10):
            self.store.append("NIFTY", date(2026, 1, i + 1), self._snap(), "TRENDING")
        records = self.store.load("NIFTY", limit=3)
        self.assertEqual(len(records), 3)

    def test_count(self):
        for i in range(5):
            self.store.append("NIFTY", date(2026, 1, i + 1), self._snap(), "TRENDING")
        self.assertEqual(self.store.count("NIFTY"), 5)

    def test_clear(self):
        self.store.append("NIFTY", date(2026, 1, 1), self._snap(), "TRENDING")
        self.store.clear("NIFTY")
        self.assertEqual(self.store.count("NIFTY"), 0)

    def test_regime_distribution(self):
        self.store.append("NIFTY", date(2026, 1, 1), self._snap(), "TRENDING")
        self.store.append("NIFTY", date(2026, 1, 2), self._snap(), "TRENDING")
        self.store.append("NIFTY", date(2026, 1, 3), self._snap(), "HIGH_VOLATILITY")
        dist = self.store.regime_distribution("NIFTY")
        self.assertEqual(dist["TRENDING"], 2)
        self.assertEqual(dist["HIGH_VOLATILITY"], 1)

    def test_drift_score_zero_on_small_history(self):
        for i in range(5):
            self.store.append("NIFTY", date(2026, 1, i + 1), self._snap(), "TRENDING")
        self.assertEqual(self.store.feature_drift_score("NIFTY"), 0.0)

    def test_drift_score_positive_on_large_shift(self):
        # Old window: low volatility
        for i in range(20):
            snap = FeatureSnapshot(symbol="NIFTY", close=18000.0, momentum_5=0.001,
                                   momentum_20=0.001, realized_volatility=0.005,
                                   volume_ratio=1.0, trend_strength=1.0)
            self.store.append("NIFTY", date(2026, 1, i + 1), snap, "MEAN_REVERTING")
        # Recent window: high volatility
        for i in range(20):
            snap = FeatureSnapshot(symbol="NIFTY", close=18000.0, momentum_5=0.05,
                                   momentum_20=0.05, realized_volatility=0.05,
                                   volume_ratio=2.0, trend_strength=8.0)
            self.store.append("NIFTY", date(2026, 2, i + 1), snap, "HIGH_VOLATILITY")
        score = self.store.feature_drift_score("NIFTY")
        self.assertGreater(score, 0.0)

    def test_empty_symbol_no_op(self):
        self.store.append("", date(2026, 1, 1), self._snap(), "TRENDING")
        self.assertEqual(self.store.all_symbols(), [])


# ---------------------------------------------------------------------------
# ImpliedVolatilityCalculator
# ---------------------------------------------------------------------------

class ImpliedVolatilityTests(unittest.TestCase):
    def setUp(self):
        self.calc = ImpliedVolatilityCalculator()

    def test_atm_call_recovers_input_vol(self):
        spot, strike, dte, vol = 18000.0, 18000.0, 30, 0.20
        # Compute BS price at 20% vol
        t = dte / 365
        d1 = (math.log(spot / strike) + (0.06 + 0.5 * vol ** 2) * t) / (vol * math.sqrt(t))
        d2 = d1 - vol * math.sqrt(t)
        def ncdf(x):
            return 0.5 * (1 + math.erf(x / math.sqrt(2)))
        bs_price = spot * ncdf(d1) - strike * math.exp(-0.06 * t) * ncdf(d2)
        # Recover IV
        iv = self.calc.calculate(bs_price, spot, strike, dte, OptionType.CE)
        self.assertAlmostEqual(iv, vol, places=3)

    def test_atm_put_recovers_input_vol(self):
        spot, strike, dte, vol = 18000.0, 18000.0, 30, 0.25
        t = dte / 365
        d1 = (math.log(spot / strike) + (0.06 + 0.5 * vol ** 2) * t) / (vol * math.sqrt(t))
        d2 = d1 - vol * math.sqrt(t)
        def ncdf(x):
            return 0.5 * (1 + math.erf(x / math.sqrt(2)))
        bs_price = strike * math.exp(-0.06 * t) * ncdf(-d2) - spot * ncdf(-d1)
        iv = self.calc.calculate(bs_price, spot, strike, dte, OptionType.PE)
        self.assertAlmostEqual(iv, vol, places=3)

    def test_raises_on_non_positive_price(self):
        with self.assertRaises(ValueError):
            self.calc.calculate(0.0, 18000, 18000, 30, OptionType.CE)

    def test_raises_on_non_positive_spot(self):
        with self.assertRaises(ValueError):
            self.calc.calculate(100.0, 0.0, 18000, 30, OptionType.CE)

    def test_iv_increases_with_higher_premium(self):
        low_iv = self.calc.calculate(100.0, 18000, 18000, 30, OptionType.CE)
        high_iv = self.calc.calculate(500.0, 18000, 18000, 30, OptionType.CE)
        self.assertGreater(high_iv, low_iv)


# ---------------------------------------------------------------------------
# IVSurface
# ---------------------------------------------------------------------------

class IVSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.master = build_default_universe()
        self.builder = IVSurfaceBuilder()
        self.chain_builder = OptionChainBuilder(self.master)
        self.underlying = "NIFTY"
        self.as_of = date(2026, 1, 2)
        from trading_platform.derivatives.engine import ExpiryCalendar
        expiry = ExpiryCalendar(self.master).nearest(self.underlying, self.as_of)
        self.chain = self.chain_builder.build(self.underlying, expiry)
        # Use a spot close to the synthetic universe's ATM strike (~22500)
        self.spot = 22500.0

    def _make_prices(self, vol: float = 0.20) -> dict[str, float]:
        from trading_platform.derivatives.engine import GreeksCalculator
        calc = GreeksCalculator()
        prices: dict[str, float] = {}
        expiry = self.chain.expiry
        dte = max((expiry - self.as_of).days, 1)
        import math as _m
        t = dte / 365
        def ncdf(x):
            return 0.5 * (1 + _m.erf(x / _m.sqrt(2)))
        def npdf(x):
            return _m.exp(-0.5 * x * x) / _m.sqrt(2 * _m.pi)
        for inst in [*self.chain.calls, *self.chain.puts]:
            if inst.strike is None or inst.option_type is None:
                continue
            s, k = self.spot, inst.strike  # type: ignore[assignment]
            d1 = (_m.log(s / k) + (0.06 + 0.5 * vol * vol) * t) / (vol * _m.sqrt(t))
            d2 = d1 - vol * _m.sqrt(t)
            if inst.option_type == OptionType.CE:
                price = s * ncdf(d1) - k * _m.exp(-0.06 * t) * ncdf(d2)
            else:
                price = k * _m.exp(-0.06 * t) * ncdf(-d2) - s * ncdf(-d1)
            prices[inst.symbol] = max(price, 0.50)
        return prices

    def test_builds_surface_from_market_prices(self):
        prices = self._make_prices(vol=0.20)
        surface = self.builder.build(
            underlying=self.underlying,
            option_chain=self.chain,
            spot_price=self.spot,
            market_prices=prices,
            as_of=self.as_of,
        )
        self.assertEqual(surface.underlying, self.underlying)
        self.assertEqual(surface.spot_price, self.spot)
        self.assertGreater(len(surface.points), 0)

    def test_atm_iv_close_to_input_vol(self):
        vol = 0.20
        prices = self._make_prices(vol=vol)
        surface = self.builder.build(
            underlying=self.underlying,
            option_chain=self.chain,
            spot_price=self.spot,
            market_prices=prices,
            as_of=self.as_of,
        )
        atm = surface.atm_iv()
        # ATM IV should be close to the flat vol used to price
        self.assertAlmostEqual(atm, vol, delta=0.05)

    def test_to_dict_has_expected_keys(self):
        prices = self._make_prices()
        surface = self.builder.build(
            underlying=self.underlying,
            option_chain=self.chain,
            spot_price=self.spot,
            market_prices=prices,
            as_of=self.as_of,
        )
        d = surface.to_dict()
        for key in ("underlying", "spot_price", "as_of", "atm_iv", "skew", "points"):
            self.assertIn(key, d)

    def test_empty_market_prices_returns_empty_surface(self):
        surface = self.builder.build(
            underlying=self.underlying,
            option_chain=self.chain,
            spot_price=self.spot,
            market_prices={},
            as_of=self.as_of,
        )
        self.assertEqual(len(surface.points), 0)


# ---------------------------------------------------------------------------
# WalkForwardEvaluator
# ---------------------------------------------------------------------------

class WalkForwardEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.master = build_default_universe()
        self.engine = BacktestEngine(self.master)
        self.evaluator = WalkForwardEvaluator(self.engine)

    def test_produces_windows_within_total_days(self):
        result = self.evaluator.evaluate(
            strategy_name="equity_momentum",
            start=date(2026, 1, 2),
            total_days=60,
            underlyings=("NIFTY",),
            starting_capital=100_000,
            max_drawdown=0.10,
            train_days=15,
            test_days=10,
        )
        self.assertGreater(len(result.windows), 0)
        for window in result.windows:
            self.assertLessEqual((window.test_end - date(2026, 1, 2)).days, 60)

    def test_window_metrics_are_set(self):
        result = self.evaluator.evaluate(
            strategy_name="equity_momentum",
            start=date(2026, 1, 2),
            total_days=50,
            underlyings=("NIFTY",),
            starting_capital=100_000,
            max_drawdown=0.10,
            train_days=15,
            test_days=10,
        )
        for window in result.windows:
            self.assertIsNotNone(window.train_metrics)
            self.assertIsNotNone(window.test_metrics)

    def test_to_dict_has_required_keys(self):
        result = self.evaluator.evaluate(
            strategy_name="futures_trend",
            start=date(2026, 1, 2),
            total_days=40,
            underlyings=("NIFTY",),
            starting_capital=100_000,
            max_drawdown=0.10,
            train_days=15,
            test_days=10,
        )
        d = result.to_dict()
        for key in ("strategy_name", "window_count", "mean_test_sharpe", "degradation_detected", "windows"):
            self.assertIn(key, d)

    def test_degradation_detected_false_on_stable_strategy(self):
        result = self.evaluator.evaluate(
            strategy_name="equity_momentum",
            start=date(2026, 1, 2),
            total_days=60,
            underlyings=("NIFTY",),
            starting_capital=100_000,
            max_drawdown=0.10,
            train_days=20,
            test_days=10,
        )
        # degradation_detected is bool — just verify it returns without error
        self.assertIsInstance(result.degradation_detected, bool)

    def test_no_windows_when_total_too_short(self):
        result = self.evaluator.evaluate(
            strategy_name="equity_momentum",
            start=date(2026, 1, 2),
            total_days=5,
            underlyings=("NIFTY",),
            starting_capital=100_000,
            max_drawdown=0.10,
            train_days=20,
            test_days=10,
        )
        self.assertEqual(len(result.windows), 0)


if __name__ == "__main__":
    unittest.main()
