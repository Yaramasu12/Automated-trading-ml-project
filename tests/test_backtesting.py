from __future__ import annotations

import unittest
from datetime import date

from trading_platform.backtesting.engine import BacktestConfig, BacktestEngine


class BacktestEngineTests(unittest.TestCase):
    def test_runs_one_month_multi_asset_backtest(self):
        result = BacktestEngine().run(
            BacktestConfig(
                starting_capital=1_000_000,
                start=date(2026, 1, 1),
                days=30,
                underlyings=("NIFTY", "BANKNIFTY", "MIDCPNIFTY", "RELIANCE"),
            )
        )

        self.assertGreaterEqual(result.metrics.trade_count, 1)
        self.assertLessEqual(result.metrics.max_drawdown, 0.10)
        self.assertIn("NIFTY", result.selected_strategies)
        payload = result.to_dict()
        self.assertEqual(payload["config"]["start"], "2026-01-01")


if __name__ == "__main__":
    unittest.main()

