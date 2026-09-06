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
        self.assertLessEqual(result.metrics.max_drawdown, 0.15)
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

    def test_cash_family_strategy_on_commodity_underlying_does_not_crash(self):
        """Regression guard: GOLD (and every other MCX commodity) has no cash/
        spot listing on this platform — only futures contracts (see
        data/instrument_master.py's MCX_COMMODITIES). Before this fix,
        _select_instrument()'s cash-family branch called
        instrument_master.get("GOLD") unconditionally and KeyError'd, since
        "GOLD" was never registered as a bare cash instrument. This is exactly
        what broke /performance/summary and /api/v1/performance in production
        (they evaluate every SCAN_UNDERLYINGS entry, commodities included,
        against every configured strategy) — and was independently reachable
        from the LIVE scan loop too, since StrategySelectionAgent.choose()
        picks strategies purely by regime and never looks at the underlying,
        so a cash-family strategy like equity_momentum can be selected for
        GOLD in real trading, not just this synthetic evaluation."""
        result = BacktestEngine().run(
            BacktestConfig(
                starting_capital=1_000_000,
                start=date(2026, 1, 1),
                days=30,
                underlyings=("GOLD",),
                strategy_names=("equity_momentum",),   # cash-family, not futures/options
            )
        )
        # Must not raise. A future-routed instrument, not a crash.
        self.assertIn("GOLD", result.selected_strategies)

    def test_evaluator_survives_full_default_universe_and_strategy_set(self):
        """Regression guard for the actual production crash: /performance/summary
        and /api/v1/performance call evaluate() with NO underlyings/strategy_names
        override, which defaults to every SCAN_UNDERLYINGS entry (commodities
        included) against every registered strategy family. Confirmed live
        2026-09-06 this crashed twice in a row on two different root causes
        (GOLD: bare cash lookup on a commodity with no cash listing; SILVERMIC:
        an options-family strategy on a commodity with no options market at
        all) — the fix generalizes to "skip a strategy/underlying combination
        that genuinely can't resolve an instrument" rather than enumerating
        every specific incompatible pair, so this must survive the real
        default strategy roster, not just one hand-picked pairing."""
        from trading_platform.strategies.factory import StrategyFactory

        all_strategy_names = tuple(StrategyFactory().names())
        result = StrategyEvaluator(BacktestEngine()).evaluate(
            start=date(2026, 1, 1),
            days=10,
            underlyings=("GOLD", "SILVERMIC", "NIFTY", "RELIANCE"),
            starting_capital=1_000_000,
            max_drawdown=0.10,
            strategy_names=all_strategy_names,
        )
        payload = result.to_dict()
        self.assertEqual(len(payload["leaderboard"]), len(all_strategy_names))

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
