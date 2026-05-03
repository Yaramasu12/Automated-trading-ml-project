from __future__ import annotations

import unittest
from datetime import date

from trading_platform.data.instrument_master import build_default_universe
from trading_platform.derivatives.engine import ContractSelector, ExpiryCalendar, GreeksCalculator, OptionChainBuilder, RolloverPlanner
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
        self.assertIn(48500.0, chain.strikes)
        self.assertIn(48500.0, chain.liquid_strikes(48500))

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


if __name__ == "__main__":
    unittest.main()
