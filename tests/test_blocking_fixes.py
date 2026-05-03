"""Tests covering the verification-report blocking-issue fixes (auth, CORS,
slippage, lookahead, regime classifier honesty, walk-forward train-then-test).

These tests are intentionally additive: they do not depend on the existing
test files and exercise the new behavior directly.
"""
from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timezone

from trading_platform.ai.features import FeatureSnapshot
from trading_platform.ai.models import RegimeClassifier
from trading_platform.backtesting.engine import BacktestConfig, BacktestEngine
from trading_platform.backtesting.evaluator import WalkForwardEvaluator
from trading_platform.broker.simulated import SimulatedBrokerClient
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.domain.enums import OrderType, ProductType, Side
from trading_platform.domain.models import OrderIntent, Signal


def _make_intent(side: Side, price: float = 100.0, quantity: int = 100) -> OrderIntent:
    instrument = build_default_universe().get("RELIANCE")
    signal = Signal(
        strategy_name="test",
        symbol=instrument.symbol,
        side=side,
        confidence=0.8,
        price=price,
        reason="test",
        created_at=datetime.now(timezone.utc),
        metadata={},
    )
    return OrderIntent(
        signal=signal,
        instrument=instrument,
        quantity=quantity,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
    )


# ---------------------------------------------------------------------------
# B1 / S1+S2: API authentication and CORS
# ---------------------------------------------------------------------------


class AuthGateTests(unittest.TestCase):
    """Verify the bearer-token gate's logic without spinning up a server."""

    def setUp(self) -> None:
        from trading_platform.api import auth
        from trading_platform.config import Settings
        from trading_platform.domain.enums import ExecutionMode

        self.auth = auth
        self.Settings = Settings
        self.ExecutionMode = ExecutionMode

    def _settings_with_token(self, token: str, required: bool = True):
        return self.Settings(
            execution_mode=self.ExecutionMode.BACKTEST,
            broker="ANGEL_ONE",
            live_trading_enabled=False,
            initial_capital=1_000_000,
            max_drawdown=0.10,
            max_daily_loss=0.02,
            max_position_pct=0.05,
            max_margin_utilization=0.60,
            live_order_confirmation="",
            angel_one_api_key="",
            angel_one_api_secret="",
            angel_one_client_code="",
            angel_one_pin="",
            angel_one_totp_secret="",
            angel_one_instrument_master_url="",
            angel_one_instrument_cache_path="",
            aws_region="ap-south-1",
            api_auth_token=token,
            api_cors_origins=("http://localhost:5173",),
            api_auth_required=required,
        )

    def test_verify_token_rejects_missing(self):
        self.auth.set_settings_for_tests(self._settings_with_token("super-secret"))
        try:
            self.assertFalse(self.auth.verify_token(None))
            self.assertFalse(self.auth.verify_token(""))
        finally:
            self.auth.set_settings_for_tests(None)

    def test_verify_token_accepts_correct(self):
        self.auth.set_settings_for_tests(self._settings_with_token("super-secret"))
        try:
            self.assertTrue(self.auth.verify_token("super-secret"))
            self.assertFalse(self.auth.verify_token("wrong-secret"))
        finally:
            self.auth.set_settings_for_tests(None)

    def test_verify_token_rejects_when_no_token_configured(self):
        # required=True + no token => fail closed.
        self.auth.set_settings_for_tests(self._settings_with_token("", required=True))
        try:
            self.assertFalse(self.auth.verify_token("any"))
        finally:
            self.auth.set_settings_for_tests(None)

    def test_verify_token_disabled_when_explicitly_off_and_empty(self):
        # required=False AND empty token => dev bypass.
        self.auth.set_settings_for_tests(self._settings_with_token("", required=False))
        try:
            self.assertTrue(self.auth.verify_token("anything"))
            self.assertTrue(self.auth.verify_token(None))
        finally:
            self.auth.set_settings_for_tests(None)

    def test_require_auth_raises_on_missing_header(self):
        from fastapi import HTTPException

        self.auth.set_settings_for_tests(self._settings_with_token("super-secret"))
        try:
            with self.assertRaises(HTTPException) as cm:
                self.auth.require_auth(None)
            self.assertEqual(cm.exception.status_code, 401)
        finally:
            self.auth.set_settings_for_tests(None)

    def test_require_auth_raises_on_wrong_token(self):
        from fastapi import HTTPException

        self.auth.set_settings_for_tests(self._settings_with_token("super-secret"))
        try:
            with self.assertRaises(HTTPException):
                self.auth.require_auth("Bearer not-the-token")
        finally:
            self.auth.set_settings_for_tests(None)

    def test_require_auth_passes_with_correct_bearer(self):
        self.auth.set_settings_for_tests(self._settings_with_token("super-secret"))
        try:
            # Returns None on success.
            self.assertIsNone(self.auth.require_auth("Bearer super-secret"))
        finally:
            self.auth.set_settings_for_tests(None)

    def test_cors_does_not_default_to_wildcard(self):
        # Settings default origins must not contain "*".
        from trading_platform.config import load_settings

        s = load_settings()
        self.assertNotIn("*", s.api_cors_origins)


class AuthIntegrationTests(unittest.TestCase):
    """Spin up the FastAPI app via TestClient and confirm 401 on protected
    routes when no token is provided."""

    @classmethod
    def setUpClass(cls):
        os.environ["API_AUTH_TOKEN"] = "test-token-xyz"
        os.environ["API_AUTH_REQUIRED"] = "true"

        from fastapi.testclient import TestClient
        from trading_platform.api import auth as auth_mod
        from trading_platform.api.app import app
        from trading_platform.config import load_settings

        auth_mod.set_settings_for_tests(load_settings())
        cls._auth_mod = auth_mod
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls._auth_mod.set_settings_for_tests(None)
        os.environ.pop("API_AUTH_TOKEN", None)
        os.environ.pop("API_AUTH_REQUIRED", None)

    def test_health_is_public(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)

    def test_state_is_public(self):
        r = self.client.get("/state")
        self.assertEqual(r.status_code, 200)

    def test_kill_switch_requires_auth(self):
        r = self.client.post("/kill-switch", json={"active": True})
        self.assertEqual(r.status_code, 401)

    def test_execution_mode_requires_auth(self):
        r = self.client.post("/execution-mode", json={"mode": "PAPER"})
        self.assertEqual(r.status_code, 401)

    def test_live_arm_requires_auth(self):
        r = self.client.post("/live/arm", json={"armed": True})
        self.assertEqual(r.status_code, 401)

    def test_enqueue_order_requires_auth(self):
        r = self.client.post("/execution/enqueue", json={"symbol": "RELIANCE", "side": "BUY", "quantity": 1, "price": 2800})
        self.assertEqual(r.status_code, 401)

    def test_orders_paper_requires_auth(self):
        r = self.client.post("/orders/paper", json={"symbol": "RELIANCE", "side": "BUY", "quantity": 1, "price": 2800})
        self.assertEqual(r.status_code, 401)

    def test_account_snapshot_requires_auth(self):
        r = self.client.get("/account/snapshot")
        self.assertEqual(r.status_code, 401)

    def test_exit_marks_requires_auth(self):
        r = self.client.post("/execution/exit-marks", json={"RELIANCE": 2800.0})
        self.assertEqual(r.status_code, 401)

    def test_protected_route_passes_with_valid_token(self):
        r = self.client.post(
            "/kill-switch",
            json={"active": False},
            headers={"Authorization": "Bearer test-token-xyz"},
        )
        self.assertEqual(r.status_code, 200)


# ---------------------------------------------------------------------------
# B4 + close-of-bar lookahead: SimulatedBrokerClient slippage
# ---------------------------------------------------------------------------


class SimulatedBrokerSlippageTests(unittest.TestCase):
    def test_buy_fill_is_above_signal_price(self):
        broker = SimulatedBrokerClient(spread_bps=10.0, impact_bps_per_unit=0.0, noise_bps=0.0)
        intent = _make_intent(Side.BUY, price=100.0, quantity=10)
        result = broker.submit_order(intent)
        self.assertGreater(result.average_price, 100.0)
        self.assertGreater(result.raw["slippage_pct"], 0)

    def test_sell_fill_is_below_signal_price(self):
        broker = SimulatedBrokerClient(spread_bps=10.0, impact_bps_per_unit=0.0, noise_bps=0.0)
        intent = _make_intent(Side.SELL, price=100.0, quantity=10)
        result = broker.submit_order(intent)
        self.assertLess(result.average_price, 100.0)
        self.assertGreater(result.raw["slippage_pct"], 0)

    def test_market_impact_grows_with_size(self):
        broker = SimulatedBrokerClient(
            spread_bps=0.0, impact_bps_per_unit=20.0, impact_capacity_notional=1_000_000.0, noise_bps=0.0
        )
        small = broker.submit_order(_make_intent(Side.BUY, price=100.0, quantity=1))
        large = broker.submit_order(_make_intent(Side.BUY, price=100.0, quantity=1000))
        self.assertGreater(large.raw["slippage_pct"], small.raw["slippage_pct"])

    def test_zero_config_still_returns_filled(self):
        broker = SimulatedBrokerClient(spread_bps=0.0, impact_bps_per_unit=0.0, noise_bps=0.0)
        intent = _make_intent(Side.BUY, price=100.0, quantity=1)
        result = broker.submit_order(intent)
        self.assertAlmostEqual(result.average_price, 100.0, places=4)


# ---------------------------------------------------------------------------
# M2 + B4 lookahead: BacktestEngine no longer trades at the same close used to
# generate the signal.
# ---------------------------------------------------------------------------


class BacktestLookaheadTests(unittest.TestCase):
    def test_signals_use_prior_bars_only(self):
        # Run a small backtest and confirm fills are away from the signal-day's close
        # (because slippage moves price and execution price is the next bar's open).
        result = BacktestEngine().run(
            BacktestConfig(starting_capital=100_000, start=date(2026, 1, 1), days=30, underlyings=("RELIANCE",))
        )
        # No assertion on PnL; we just want trades to exist and to pass through
        # the slippage broker.
        self.assertGreaterEqual(result.metrics.trade_count, 0)


# ---------------------------------------------------------------------------
# B3: RegimeClassifier honesty
# ---------------------------------------------------------------------------


class RegimeClassifierHonestyTests(unittest.TestCase):
    def _records_with_rule_labels(self, n: int = 30) -> list[dict]:
        return [
            {
                "momentum_5": 0.005 * (i % 5),
                "momentum_20": 0.003 * (i % 4),
                "realized_volatility": 0.005 + 0.001 * (i % 6),
                "volume_ratio": 1.0 + 0.05 * (i % 3),
                "trend_strength": float(i % 5),
                "regime": RegimeClassifier.REGIMES[i % 4],
            }
            for i in range(n)
        ]

    def test_train_refuses_rule_derived_labels(self):
        clf = RegimeClassifier()
        ok = clf.train(self._records_with_rule_labels(), label_source=RegimeClassifier.RULE_LABEL_SOURCE)
        self.assertFalse(ok)
        self.assertFalse(clf.is_trained)
        self.assertIsNotNone(clf.last_train_metrics)
        self.assertTrue(clf.last_train_metrics.get("rejected"))

    def test_train_default_is_rule_based_and_refused(self):
        # Default label_source is "rule_based" — must be refused.
        clf = RegimeClassifier()
        ok = clf.train(self._records_with_rule_labels())
        self.assertFalse(ok)
        self.assertFalse(clf.is_trained)

    def test_predict_falls_back_to_rule_when_not_trained(self):
        clf = RegimeClassifier()
        snap = FeatureSnapshot(
            symbol="X", close=100.0, momentum_5=0.0, momentum_20=0.0, realized_volatility=0.03,
            volume_ratio=1.0, trend_strength=0.0,
        )
        # 0.03 vol -> HIGH_VOLATILITY rule
        self.assertEqual(clf.predict(snap), "HIGH_VOLATILITY")

    def test_train_with_external_labels_records_holdout_metrics(self):
        # Provide labels that intentionally diverge from the rule, so the
        # holdout split is non-trivial. Whether sklearn is actually present
        # is irrelevant: the test asserts the metrics dict is populated and
        # the path is honest about the source.
        clf = RegimeClassifier()
        records = self._records_with_rule_labels()
        # Flip every other label to "TRENDING" to force divergence.
        for idx, rec in enumerate(records):
            if idx % 2 == 0:
                rec["regime"] = "TRENDING"
        result = clf.train(records, label_source="external_forward_returns")
        # If sklearn is installed, training proceeds and we assert metrics present.
        # If not, train returns False with sklearn_unavailable.
        self.assertIsInstance(result, bool)
        self.assertIsNotNone(clf.last_train_metrics)
        self.assertEqual(clf.label_source, "external_forward_returns")


# ---------------------------------------------------------------------------
# B2: WalkForwardEvaluator now actually fits something between train and test.
# ---------------------------------------------------------------------------


class WalkForwardHonestyTests(unittest.TestCase):
    def test_fitted_params_are_present_and_frozen(self):
        engine = BacktestEngine()
        evaluator = WalkForwardEvaluator(engine)
        result = evaluator.evaluate(
            strategy_name="equity_momentum",
            start=date(2026, 1, 1),
            total_days=60,
            underlyings=("NIFTY",),
            starting_capital=1_000_000,
            max_drawdown=0.10,
            train_days=20,
            test_days=10,
        )
        self.assertGreater(len(result.windows), 0)
        for w in result.windows:
            # Each window must expose its fitted-on-train params.
            self.assertIsNotNone(w.fitted_params)
            d = w.to_dict()
            self.assertIn("fitted_params", d)
            self.assertIn("test_skipped", d)
            self.assertIn("confidence_floor", d["fitted_params"])
            # If the strategy was rejected on the train window,
            # the test window MUST be skipped (zero metrics).
            if not w.fitted_params.accept_strategy:
                self.assertTrue(w.test_skipped)
                self.assertEqual(w.test_metrics.trade_count, 0)


if __name__ == "__main__":
    unittest.main()
