from __future__ import annotations

import unittest
from datetime import date

from trading_platform.data.instrument_master import build_default_universe
from trading_platform.derivatives.engine import (
    MIN_IV_RANK_OBSERVATIONS,
    ContractSelector,
    ExpiryCalendar,
    GreeksCalculator,
    ImpliedVolatilityCalculator,
    OptionChainBuilder,
    RolloverPlanner,
    black_scholes_price,
    compute_iv_rank,
)
from trading_platform.domain.enums import InstrumentType, OptionType


class DerivativesEngineTests(unittest.TestCase):
    def setUp(self):
        self.master = build_default_universe(date(2026, 1, 5))

    def test_expiry_calendar_finds_nearest_and_next(self):
        calendar = ExpiryCalendar(self.master)

        nearest = calendar.nearest("NIFTY", date(2026, 1, 5))
        next_expiry = calendar.next_after_nearest("NIFTY", date(2026, 1, 5))

        self.assertGreaterEqual(nearest, date(2026, 1, 5))
        self.assertGreater(next_expiry, nearest)

    def test_option_chain_groups_calls_and_puts(self):
        expiry = ExpiryCalendar(self.master).nearest("BANKNIFTY", date(2026, 1, 5))
        chain = OptionChainBuilder(self.master).build("BANKNIFTY", expiry)

        self.assertGreater(len(chain.calls), 0)
        self.assertEqual(len(chain.calls), len(chain.puts))
        self.assertIn(54400.0, chain.strikes)
        self.assertIn(54400.0, chain.liquid_strikes(54400))

    def test_contract_selector_selects_future_and_option(self):
        selector = ContractSelector(self.master)

        future = selector.select_future("NIFTY", date(2026, 1, 5))
        option = selector.select_option("NIFTY", date(2026, 1, 5), 22500, OptionType.CE)

        self.assertEqual(future.instrument_type, InstrumentType.FUTURE)
        self.assertEqual(option.option_type, OptionType.CE)

    def test_greeks_are_directionally_sensible(self):
        greeks = GreeksCalculator().calculate(spot_price=22500, strike=22500, days_to_expiry=7, volatility=0.18, option_type=OptionType.CE)

        self.assertGreater(greeks.delta, 0)
        self.assertGreater(greeks.gamma, 0)
        self.assertGreater(greeks.vega, 0)

    def test_rollover_planner_rolls_when_strategy_allows(self):
        current = ContractSelector(self.master).select_future("NIFTY", date(2026, 1, 5))
        plan = RolloverPlanner(self.master).plan(current, current.expiry, allow_rollover=True)

        self.assertEqual(plan.action, "ROLL")
        self.assertIsNotNone(plan.next_contract)


class IVRankTests(unittest.TestCase):
    def test_insufficient_history_returns_none(self):
        history = [15.0] * (MIN_IV_RANK_OBSERVATIONS - 1)
        self.assertIsNone(compute_iv_rank(15.0, history))

    def test_exactly_minimum_history_computes(self):
        history = list(range(10, 10 + MIN_IV_RANK_OBSERVATIONS))  # 10..29
        result = compute_iv_rank(29.0, [float(v) for v in history])
        self.assertIsNotNone(result)
        self.assertEqual(result.lookback_n, MIN_IV_RANK_OBSERVATIONS)

    def test_current_at_lookback_high_gives_rank_100(self):
        history = [float(v) for v in range(10, 10 + MIN_IV_RANK_OBSERVATIONS)]  # 10..29
        result = compute_iv_rank(29.0, history)
        self.assertEqual(result.rank, 100.0)

    def test_current_at_lookback_low_gives_rank_0(self):
        history = [float(v) for v in range(10, 10 + MIN_IV_RANK_OBSERVATIONS)]
        result = compute_iv_rank(10.0, history)
        self.assertEqual(result.rank, 0.0)

    def test_current_at_midpoint_gives_rank_50(self):
        history = [10.0, 20.0] * MIN_IV_RANK_OBSERVATIONS
        result = compute_iv_rank(15.0, history)
        self.assertEqual(result.rank, 50.0)

    def test_rank_and_percentile_diverge_on_skewed_distribution(self):
        # One extreme spike (100) compresses rank for a current value (20)
        # that is nonetheless above nearly the whole rest of the distribution.
        history = [10.0] * (MIN_IV_RANK_OBSERVATIONS - 1) + [100.0]
        result = compute_iv_rank(20.0, history)
        self.assertLess(result.rank, 20.0)          # (20-10)/(100-10) ~= 11%
        self.assertGreaterEqual(result.percentile, 90.0)  # above all the 10.0s

    def test_current_outside_history_range_is_clamped_not_extrapolated(self):
        history = [float(v) for v in range(10, 10 + MIN_IV_RANK_OBSERVATIONS)]
        result = compute_iv_rank(1000.0, history)
        self.assertEqual(result.rank, 100.0)   # clamped, not >100
        self.assertEqual(result.percentile, 100.0)

    def test_flat_history_does_not_divide_by_zero(self):
        history = [15.0] * MIN_IV_RANK_OBSERVATIONS
        result = compute_iv_rank(15.0, history)
        self.assertEqual(result.rank, 50.0)   # no range to place it within -> neutral

    def test_nonpositive_or_missing_values_are_filtered_out(self):
        history = [float(v) for v in range(10, 10 + MIN_IV_RANK_OBSERVATIONS)] + [0.0, -5.0, None]  # type: ignore[list-item]
        result = compute_iv_rank(29.0, history)
        self.assertEqual(result.lookback_n, MIN_IV_RANK_OBSERVATIONS)

    def test_nonpositive_current_returns_none(self):
        history = [float(v) for v in range(10, 10 + MIN_IV_RANK_OBSERVATIONS)]
        self.assertIsNone(compute_iv_rank(0.0, history))


class BlackScholesPriceTests(unittest.TestCase):
    def test_matches_iv_calculator_round_trip(self):
        # A price -> IV -> price round trip through the two independent
        # implementations (ImpliedVolatilityCalculator's private _bs_price
        # and this public function) must agree on the same inputs.
        spot, strike, dte, ot = 24000.0, 24000.0, 7, OptionType.CE
        true_price = 150.0
        iv = ImpliedVolatilityCalculator().calculate(true_price, spot, strike, dte, ot)
        recovered = black_scholes_price(spot, strike, dte / 365.0, iv, ot)
        self.assertAlmostEqual(recovered, true_price, places=2)

    def test_call_price_increases_with_spot(self):
        low = black_scholes_price(23000, 24000, 7 / 365, 0.15, OptionType.CE)
        high = black_scholes_price(25000, 24000, 7 / 365, 0.15, OptionType.CE)
        self.assertLess(low, high)

    def test_put_price_decreases_with_spot(self):
        low = black_scholes_price(23000, 24000, 7 / 365, 0.15, OptionType.PE)
        high = black_scholes_price(25000, 24000, 7 / 365, 0.15, OptionType.PE)
        self.assertGreater(low, high)

    def test_zero_time_returns_intrinsic_value(self):
        self.assertAlmostEqual(black_scholes_price(24500, 24000, 0.0, 0.15, OptionType.CE), 500.0)
        self.assertAlmostEqual(black_scholes_price(23500, 24000, 0.0, 0.15, OptionType.CE), 0.0)
        self.assertAlmostEqual(black_scholes_price(23500, 24000, 0.0, 0.15, OptionType.PE), 500.0)

    def test_zero_volatility_returns_intrinsic_value(self):
        self.assertAlmostEqual(black_scholes_price(24500, 24000, 7 / 365, 0.0, OptionType.CE), 500.0)

    def test_rejects_nonpositive_spot_or_strike(self):
        with self.assertRaises(ValueError):
            black_scholes_price(0.0, 24000, 7 / 365, 0.15, OptionType.CE)
        with self.assertRaises(ValueError):
            black_scholes_price(24000, -1.0, 7 / 365, 0.15, OptionType.CE)


if __name__ == "__main__":
    unittest.main()
