"""Tests covering the verification-report non-blocking issue fixes:

- N2 (backtest demo non-degenerate)
- N3/M5 (GARCH/volatility out-of-sample evaluation)
- N4 (BacktestEngine universe rebuild caching)
- N5 (MetaModel EMA initialisation honesty)
- N7 (RegimeClassifier defensive label validation)
- N8 (option final-bar liquidation: BS-based pricing instead of 1.5%-of-spot)

These tests are additive and do not depend on the pre-existing test files.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta

from trading_platform.ai.features import FeatureSnapshot
from trading_platform.ai.models import (
    GARCHForecaster,
    MetaModel,
    RegimeClassifier,
    VolatilityForecaster,
    WalkForwardForecastEvaluation,
)
from trading_platform.backtesting.engine import BacktestConfig, BacktestEngine
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.domain.enums import InstrumentType, OptionType


# ---------------------------------------------------------------------------
# N2: backtest demo non-degenerate
# ---------------------------------------------------------------------------


class BacktestDemoNonDegenerateTests(unittest.TestCase):
    def test_default_demo_run_produces_non_zero_metrics(self):
        """The README's default demo backtest must not return entirely empty
        metrics. Concretely: at least one trade, a non-zero profit factor or
        win rate, and a meaningful trade count after the fix.
        """
        result = BacktestEngine().run(
            BacktestConfig(
                starting_capital=1_000_000,
                start=date(2026, 1, 1),
                days=30,
                underlyings=("NIFTY", "BANKNIFTY", "MIDCPNIFTY", "RELIANCE", "TCS"),
            )
        )
        metrics = result.metrics
        self.assertGreater(metrics.trade_count, 0)
        # At least one of {win_rate, profit_factor} must be informative.
        self.assertTrue(
            metrics.win_rate > 0.0 or metrics.profit_factor > 0.0,
            f"degenerate metrics: win_rate={metrics.win_rate} profit_factor={metrics.profit_factor}",
        )

    def test_forced_exit_is_not_blocked_by_position_size_limit(self):
        """N2 root cause: the forced end-of-backtest liquidation was rejected
        by the position-size cap. Closing orders (`opens_position=False`)
        must bypass position-growth checks because they reduce risk.
        """
        from trading_platform.domain.enums import ExecutionMode, Side
        from trading_platform.domain.models import OrderIntent, Signal
        from trading_platform.portfolio.ledger import PortfolioSnapshot
        from trading_platform.risk.engine import RiskEngine

        instrument = build_default_universe().get("NIFTY")
        signal = Signal(
            strategy_name="forced_backtest_exit",
            symbol=instrument.symbol,
            side=Side.SELL,
            confidence=1.0,
            price=25_000.0,
            reason="forced exit",
            created_at=datetime(2026, 1, 30, 15, 20),
            metadata={"opens_position": False, "hedged": True},
        )
        # Notional way over the 5% cap: 1 NIFTY future at 25000 * 50 = 1.25M
        intent = OrderIntent(
            signal=signal,
            instrument=instrument,
            quantity=1,
            order_type=__import__("trading_platform.domain.enums", fromlist=["OrderType"]).OrderType.MARKET,
            product_type=__import__("trading_platform.domain.enums", fromlist=["ProductType"]).ProductType.INTRADAY,
        )
        portfolio_snapshot = PortfolioSnapshot(
            cash=1_000_000,
            equity=1_000_000,
            realized_pnl=0,
            unrealized_pnl=0,
            drawdown=0,
            peak_equity=1_000_000,
            open_positions=1,
        )
        decision = RiskEngine().evaluate(
            intent,
            portfolio_snapshot,
            datetime(2026, 1, 30, 15, 20),
            ExecutionMode.BACKTEST,
        )
        self.assertTrue(decision.approved, msg=f"closing order rejected: {decision.reason}")


# ---------------------------------------------------------------------------
# N3 / M5: GARCH / volatility OOS evaluation
# ---------------------------------------------------------------------------


def _synthetic_closes(n: int, drift: float = 0.0005, seed: int = 7, vol: float = 0.012) -> list[float]:
    import random

    rng = random.Random(seed)
    price = 100.0
    out = [price]
    for _ in range(n - 1):
        price *= 1.0 + drift + rng.gauss(0.0, vol)
        out.append(price)
    return out


class VolatilityWalkForwardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.closes = _synthetic_closes(80)
        self.fcst = VolatilityForecaster()

    def test_walk_forward_evaluation_returns_separate_train_and_test_coverages(self):
        wf = self.fcst.walk_forward_evaluate(self.closes, model_name="ewma_volatility")
        self.assertIsInstance(wf, WalkForwardForecastEvaluation)
        self.assertGreater(wf.train_size, 0)
        self.assertGreater(wf.test_size, 0)
        self.assertGreaterEqual(wf.in_sample_coverage, 0.0)
        self.assertLessEqual(wf.in_sample_coverage, 1.0)
        self.assertGreaterEqual(wf.out_of_sample_coverage, 0.0)
        self.assertLessEqual(wf.out_of_sample_coverage, 1.0)
        # Train/test windows do not overlap (test starts at split index).
        self.assertLessEqual(wf.train_size + wf.test_size, len(self.closes) + 1)

    def test_evaluate_interval_marks_in_sample_flag(self):
        forecast = self.fcst.forecast(self.closes)
        evaluation = self.fcst.evaluate_interval(self.closes, forecast)
        self.assertTrue(evaluation.in_sample)
        oos_evaluation = self.fcst.evaluate_interval(self.closes[-10:], forecast, in_sample=False)
        self.assertFalse(oos_evaluation.in_sample)

    def test_walk_forward_requires_minimum_sample(self):
        with self.assertRaises(ValueError):
            self.fcst.walk_forward_evaluate(self.closes[:5])


class GARCHWalkForwardTests(unittest.TestCase):
    def test_garch_walk_forward_evaluation(self):
        closes = _synthetic_closes(60)
        wf = GARCHForecaster().walk_forward_evaluate(closes)
        self.assertIsInstance(wf, WalkForwardForecastEvaluation)
        self.assertEqual(wf.model_name, "garch_1_1")
        self.assertGreater(wf.train_size, 0)
        self.assertGreater(wf.test_size, 0)


# ---------------------------------------------------------------------------
# N4: BacktestEngine universe rebuild caching
# ---------------------------------------------------------------------------


class BacktestEngineUniverseCacheTests(unittest.TestCase):
    def test_default_universe_is_cached_per_start_date(self):
        engine = BacktestEngine()
        # Run twice with the same start
        result_a = engine.run(BacktestConfig(start=date(2026, 2, 1), days=22, underlyings=("NIFTY",)))
        master_a = engine.instrument_master
        result_b = engine.run(BacktestConfig(start=date(2026, 2, 1), days=22, underlyings=("NIFTY",)))
        master_b = engine.instrument_master
        self.assertIs(master_a, master_b, "default universe should be reused for same start")
        # Different start triggers a fresh build but is also cached.
        engine.run(BacktestConfig(start=date(2026, 3, 1), days=22, underlyings=("NIFTY",)))
        master_c = engine.instrument_master
        self.assertIsNot(master_a, master_c)
        self.assertIn(date(2026, 2, 1), engine._default_master_cache)
        self.assertIn(date(2026, 3, 1), engine._default_master_cache)


# ---------------------------------------------------------------------------
# N5: MetaModel EMA initialisation
# ---------------------------------------------------------------------------


class MetaModelEMAInitialisationTests(unittest.TestCase):
    def test_first_observation_is_smoothed_not_fully_retained(self):
        m = MetaModel()
        m.update("TRENDING", "x", 10.0)
        # With prev=0 seed and alpha=0.30, the score should be 3.0 not 10.0.
        ranked = m.rank("TRENDING", ["x"])
        self.assertAlmostEqual(ranked[0].score, 3.0, places=6)
        self.assertEqual(m.observation_count("TRENDING", "x"), 1)

    def test_observation_count_tracks_updates(self):
        m = MetaModel()
        for value in [10.0, 5.0, 0.0]:
            m.update("TRENDING", "x", value)
        self.assertEqual(m.observation_count("TRENDING", "x"), 3)
        self.assertEqual(m.observation_count("TRENDING", "y"), 0)
        self.assertEqual(m.observation_count("MEAN_REVERTING", "x"), 0)


# ---------------------------------------------------------------------------
# N7: RegimeClassifier defensive label validation
# ---------------------------------------------------------------------------


class _FakeModel:
    """Stand-in sklearn model that returns an unexpected label."""

    classes_ = ["UNKNOWN_REGIME"]

    def predict(self, _X):
        return ["UNKNOWN_REGIME"]

    def predict_proba(self, _X):
        return [[1.0]]


class _ExplodingModel:
    classes_ = ["TRENDING"]

    def predict(self, _X):
        raise RuntimeError("simulated sklearn failure")

    def predict_proba(self, _X):
        raise RuntimeError("simulated sklearn failure")


class RegimeClassifierDefensiveValidationTests(unittest.TestCase):
    def setUp(self):
        self.features = FeatureSnapshot(
            symbol="X",
            close=100.0,
            momentum_5=0.01,
            momentum_20=0.02,
            realized_volatility=0.015,
            volume_ratio=1.0,
            trend_strength=2.0,
        )

    def test_predict_falls_back_when_model_returns_unknown_label(self):
        clf = RegimeClassifier()
        clf._model = _FakeModel()
        clf._trained = True
        result = clf.predict(self.features)
        self.assertIn(result, RegimeClassifier.REGIMES)

    def test_predict_falls_back_when_model_raises(self):
        clf = RegimeClassifier()
        clf._model = _ExplodingModel()
        clf._trained = True
        # Should not raise.
        result = clf.predict(self.features)
        self.assertIn(result, RegimeClassifier.REGIMES)

    def test_predict_proba_drops_unknown_classes(self):
        clf = RegimeClassifier()
        clf._model = _FakeModel()
        clf._trained = True
        probs = clf.predict_proba(self.features)
        # All keys are valid regimes.
        for key in probs:
            self.assertIn(key, RegimeClassifier.REGIMES)
        # Falls back via rule when nothing valid.
        total = sum(probs.values())
        self.assertAlmostEqual(total, 1.0, places=6)


# ---------------------------------------------------------------------------
# N8: option final-bar BS pricing
# ---------------------------------------------------------------------------


class OptionMarkPricingTests(unittest.TestCase):
    def setUp(self):
        from trading_platform.derivatives.engine import ImpliedVolatilityCalculator

        self.engine = BacktestEngine()
        self.iv = ImpliedVolatilityCalculator()

    def _option(self, expiry: date, option_type: OptionType, strike: float = 22000.0):
        from trading_platform.domain.enums import AssetClass, Exchange, Segment
        from trading_platform.domain.models import Instrument

        return Instrument(
            symbol="NIFTY-TEST-OPT",
            name="NIFTY test option",
            exchange=Exchange.NFO,
            segment=Segment.OPTIONS,
            asset_class=AssetClass.INDEX,
            instrument_type=InstrumentType.OPTION,
            token="TEST",
            lot_size=50,
            tick_size=0.05,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            underlying="NIFTY",
        )

    def test_expired_call_uses_intrinsic_value(self):
        as_of = date(2026, 2, 1)
        instrument = self._option(date(2026, 1, 30), OptionType.CE, strike=22_000.0)
        underlying_marks = {"NIFTY": 22_500.0}
        price = self.engine._mark_price_for_instrument(
            instrument, underlying_marks, fallback=10.0, entry_context=None, as_of=as_of
        )
        self.assertAlmostEqual(price, 500.0, delta=0.1)

    def test_expired_otm_put_is_zero_clipped_to_tick(self):
        as_of = date(2026, 2, 1)
        instrument = self._option(date(2026, 1, 30), OptionType.PE, strike=22_000.0)
        underlying_marks = {"NIFTY": 23_000.0}
        price = self.engine._mark_price_for_instrument(
            instrument, underlying_marks, fallback=10.0, entry_context=None, as_of=as_of
        )
        # OTM put at expiry: intrinsic 0, clipped to tick floor.
        self.assertGreaterEqual(price, instrument.tick_size)
        self.assertLess(price, 1.0)

    def test_live_option_uses_bs_with_fitted_iv_when_entry_context_known(self):
        as_of = date(2026, 1, 15)
        expiry = date(2026, 1, 30)
        instrument = self._option(expiry, OptionType.CE, strike=22_000.0)
        # Entry context: 2 weeks earlier we paid 250 for the call when spot was 22,000
        entry_date = date(2026, 1, 1)
        entry_context = {instrument.symbol: (22_000.0, 250.0, entry_date)}
        underlying_marks = {"NIFTY": 22_400.0}
        price = self.engine._mark_price_for_instrument(
            instrument, underlying_marks, fallback=10.0, entry_context=entry_context, as_of=as_of
        )
        # Spot is up 400, time decayed; should be a positive, reasonable value.
        self.assertGreater(price, 0.0)
        self.assertLess(price, underlying_marks["NIFTY"])  # never exceed underlying
        # And NOT the legacy 1.5%-of-spot heuristic (336.0).
        self.assertNotAlmostEqual(price, 0.015 * underlying_marks["NIFTY"], places=2)

    def test_no_entry_context_falls_back_to_baseline_iv_bs_price(self):
        as_of = date(2026, 1, 15)
        expiry = date(2026, 1, 30)
        instrument = self._option(expiry, OptionType.CE, strike=22_000.0)
        underlying_marks = {"NIFTY": 22_000.0}
        price = self.engine._mark_price_for_instrument(
            instrument, underlying_marks, fallback=10.0, entry_context=None, as_of=as_of
        )
        # ATM call ~15 days, baseline 25% IV: a positive non-zero number.
        self.assertGreater(price, 1.0)
        self.assertLess(price, underlying_marks["NIFTY"])


if __name__ == "__main__":
    unittest.main()
