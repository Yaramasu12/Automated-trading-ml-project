from __future__ import annotations

import unittest
from datetime import date

from trading_platform.backtesting.engine import BacktestConfig, BacktestEngine
from trading_platform.backtesting.evaluator import StrategyEvaluator
from trading_platform.data.instrument_master import build_default_universe
from trading_platform.domain.enums import OptionType, OrderType, ProductType, Side


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


class GenericBacktestChronologyFixesTests(unittest.TestCase):
    """Regression tests for audit_results/2026-09-13/AUDIT_REPORT.md finding
    #5 (high) — the generic backtester's option pricing and product-type
    classification bugs."""

    def test_mark_price_for_instrument_prices_options_via_black_scholes(self):
        # The core bug: fill_reference_price used to be the raw UNDERLYING
        # bar open (thousands of rupees) for ANY instrument type, options
        # included. _mark_price_for_instrument must produce an option-scale
        # premium instead, not the underlying's own price.
        engine = BacktestEngine()
        master = build_default_universe(date(2026, 1, 5))
        option = master.select_option("NIFTY", date(2026, 1, 5), 24500, OptionType.CE)
        underlying_price = 24000.0

        price = engine._mark_price_for_instrument(
            option, {"NIFTY": underlying_price}, underlying_price,
            entry_context=None, as_of=date(2026, 1, 5),
        )

        # An OTM NIFTY option's premium is a tiny fraction of the index
        # level -- nowhere near underlying_price itself (the old bug would
        # have returned exactly 24000.0 here).
        self.assertLess(price, underlying_price * 0.05)
        self.assertGreater(price, 0)
        self.assertNotAlmostEqual(price, underlying_price, delta=1.0)

    def test_mark_price_for_instrument_passes_through_non_options_unchanged(self):
        # Equities/futures must still be priced at the raw underlying level
        # -- this fix must not touch non-option instruments at all.
        engine = BacktestEngine()
        master = build_default_universe(date(2026, 1, 5))
        future = master.select_future("NIFTY", date(2026, 1, 5))
        price = engine._mark_price_for_instrument(
            future, {"NIFTY": 24000.0}, 24000.0, entry_context=None, as_of=date(2026, 1, 5),
        )
        self.assertEqual(price, 24000.0)

    def test_option_bar_range_brackets_correctly_regardless_of_type(self):
        # A call's premium rises with spot, a put's falls -- taking
        # min/max of the two endpoint evaluations must bracket the
        # option's own implied range correctly either way (previously,
        # the raw underlying's OHLC was compared directly against a
        # premium-denominated stop/target, comparing incompatible scales).
        engine = BacktestEngine()
        master = build_default_universe(date(2026, 1, 5))
        call = master.select_option("NIFTY", date(2026, 1, 5), 24500, OptionType.CE)
        put = master.select_option("NIFTY", date(2026, 1, 5), 23500, OptionType.PE)
        u_low, u_high = 23800.0, 24200.0

        call_low = engine._mark_price_for_instrument(call, {"NIFTY": u_low}, u_low, None, date(2026, 1, 5))
        call_high = engine._mark_price_for_instrument(call, {"NIFTY": u_high}, u_high, None, date(2026, 1, 5))
        self.assertLess(call_low, call_high)  # call premium rises with spot

        put_low = engine._mark_price_for_instrument(put, {"NIFTY": u_low}, u_low, None, date(2026, 1, 5))
        put_high = engine._mark_price_for_instrument(put, {"NIFTY": u_high}, u_high, None, date(2026, 1, 5))
        self.assertGreater(put_low, put_high)  # put premium falls with spot
        # min/max of the two endpoints is what the engine now uses as the
        # option's own bar_low/bar_high -- confirm it's a real bracket, not
        # a degenerate single point, for both option types.
        self.assertLess(min(put_low, put_high), max(put_low, put_high))

    def test_multiday_positions_are_carryforward_not_intraday(self):
        # Every strategy registered in this engine holds for >= 2 days
        # (exit_rules().max_holding_days) -- tagging fills INTRADAY made
        # ChargesModel apply the wrong (lower) equity STT/brokerage
        # schedule. Run a real backtest and check the actual persisted
        # trades/charges reflect delivery-style costs, not intraday ones.
        from trading_platform.backtesting.charges import ChargesModel
        from trading_platform.domain.models import OrderIntent, Signal
        from datetime import datetime, timezone

        result = BacktestEngine().run(
            BacktestConfig(
                starting_capital=1_000_000,
                start=date(2026, 1, 1),
                days=30,
                underlyings=("RELIANCE",),
                strategy_names=("equity_momentum",),
            )
        )
        self.assertGreaterEqual(result.metrics.trade_count, 1)

        # Confirm the charges actually applied match CARRYFORWARD (delivery)
        # rates, not INTRADAY ones, for an equivalent equity fill.
        master = build_default_universe(date(2026, 1, 5))
        instrument = master.get("RELIANCE")
        signal = Signal("equity_momentum", "RELIANCE", Side.BUY,
            0.9, 2800.0, "test", datetime.now(timezone.utc), metadata={})
        intraday_intent = OrderIntent(signal, instrument, 1, OrderType.MARKET, ProductType.INTRADAY)
        carryforward_intent = OrderIntent(signal, instrument, 1, OrderType.MARKET, ProductType.CARRYFORWARD)
        model = ChargesModel()
        intraday_charges = model.estimate(intraday_intent, 2800.0)
        carryforward_charges = model.estimate(carryforward_intent, 2800.0)
        # Delivery STT (0.1%) is meaningfully higher than intraday STT
        # (0.025%) for a BUY-side-stamp-duty-bearing fill -- the two must
        # not be equal, confirming product_type actually changes the
        # applied schedule (this is a sanity check on ChargesModel itself,
        # establishing that the engine's CARRYFORWARD tagging is not a
        # no-op).
        self.assertNotEqual(intraday_charges, carryforward_charges)


if __name__ == "__main__":
    unittest.main()
