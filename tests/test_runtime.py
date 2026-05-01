from __future__ import annotations

import unittest

from trading_platform.api.runtime import TradingRuntime
from trading_platform.config import Settings
from trading_platform.domain.enums import ExecutionMode


class RuntimeTests(unittest.TestCase):
    def test_live_arm_requires_credentials_and_flag(self):
        runtime = TradingRuntime(
            Settings(
                execution_mode=ExecutionMode.LIVE,
                broker="ANGEL_ONE",
                live_trading_enabled=False,
                initial_capital=1_000_000,
                max_drawdown=0.10,
                max_daily_loss=0.02,
                max_position_pct=0.05,
                max_margin_utilization=0.60,
                angel_one_api_key="",
                angel_one_client_code="",
                angel_one_pin="",
                angel_one_totp_secret="",
                angel_one_instrument_master_url="https://example.invalid/OpenAPIScripMaster.json",
                angel_one_instrument_cache_path="data/processed/test_angel_instruments.json",
                aws_region="ap-south-1",
            )
        )

        with self.assertRaises(ValueError):
            runtime.arm_live(True)

    def test_backtest_endpoint_payload(self):
        runtime = TradingRuntime()
        result = runtime.run_backtest({"days": 30, "underlyings": ["NIFTY", "RELIANCE"]})

        self.assertIn("metrics", result)
        self.assertIn("selected_strategies", result)

    def test_retraining_decision_endpoint(self):
        runtime = TradingRuntime()
        result = runtime.retraining_decision(
            {
                "model_name": "sentiment",
                "profit_factor": 1.2,
                "sharpe": 0.7,
                "drawdown": 0.03,
                "iv_interval_coverage": 0.95,
                "sentiment_precision": 0.7,
                "sample_size": 50,
            }
        )

        self.assertTrue(result["should_retrain"])
        self.assertEqual(result["reason"], "sentiment_precision_below_threshold")

    def test_data_status_reports_cache_and_credentials(self):
        runtime = TradingRuntime()
        status = runtime.data_status()

        self.assertEqual(status["instrument_source"], "angel_one")
        self.assertIn("instrument_cache_exists", status)
        self.assertFalse(status["angel_one_configured"])


if __name__ == "__main__":
    unittest.main()
