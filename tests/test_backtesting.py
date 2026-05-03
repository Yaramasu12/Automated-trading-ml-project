from __future__ import annotations

import unittest
from datetime import date

from trading_platform.backtesting.engine import BacktestConfig, BacktestEngine
from trading_platform.backtesting.evaluator import StrategyEvaluator


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

    def test_can_force_specific_strategy_for_evaluation(self):
        result = BacktestEngine().run(
            BacktestConfig(
                starting_capital=1_000_000,
                start=date(2026, 1, 1),
                days=30,
                underlyings=("NIFTY", "RELIANCE"),
                strategy_names=("futures_trend",),
            )
        )

        self.assertEqual(result.selected_strategies["NIFTY"], ["futures_trend"])
        self.assertIn("strategy_names", result.to_dict()["config"])

    def test_strategy_evaluator_returns_ranked_leaderboard(self):
        result = StrategyEvaluator(BacktestEngine()).evaluate(
            start=date(2026, 1, 1),
            days=30,
            underlyings=("NIFTY", "RELIANCE"),
            starting_capital=1_000_000,
            max_drawdown=0.10,
            strategy_names=("futures_trend", "equity_momentum", "defined_risk_option_spread"),
        )

        payload = result.to_dict()
        self.assertEqual(len(payload["leaderboard"]), 3)
        self.assertEqual(payload["leaderboard"][0]["rank"], 1)
        self.assertIn(payload["best_strategy"], {"futures_trend", "equity_momentum", "defined_risk_option_spread"})


if __name__ == "__main__":
    unittest.main()
