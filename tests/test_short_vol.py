"""Unit tests for the defined-risk short-vol strategy logic."""
from __future__ import annotations

import math
import unittest

import numpy as np

from trading_platform.domain.enums import OptionType, Side
from trading_platform.strategies.short_vol import ShortVolStrategy


def _flat_closes(price=24000.0, n=40, daily_vol=0.006, seed=1):
    """Synthetic closes with a known daily vol -> ~annualized daily_vol*sqrt(252)."""
    rng = np.random.default_rng(seed)
    r = rng.normal(0, daily_vol, n)
    return list(price * np.exp(np.cumsum(r)))


class ShortVolTests(unittest.TestCase):
    def setUp(self):
        self.s = ShortVolStrategy(sd=1.25, wing_width=300, risk_budget=0.05, min_vrp=2.0)

    def test_realized_vol_reasonable(self):
        # daily vol 0.006 -> ann ~ 0.006*sqrt(252)*100 ~ 9.5%
        rv = ShortVolStrategy.realized_vol(_flat_closes(daily_vol=0.006))
        self.assertTrue(6.0 < rv < 14.0, rv)

    def test_no_entry_when_premium_thin(self):
        # realized ~9.5%, VIX 10 -> VRP ~0.5 < 2 -> no entry
        d = self.s.decide(spot=24000, vix=10.0, closes=_flat_closes(daily_vol=0.006),
                          capital=1_000_000, lot_size=50)
        self.assertFalse(d.enter)
        self.assertIn("premium not rich", d.reason)

    def test_entry_when_premium_rich(self):
        # realized ~9.5%, VIX 16 -> VRP ~6.5 >= 2 -> enter with a full condor
        d = self.s.decide(spot=24000, vix=16.0, closes=_flat_closes(daily_vol=0.006),
                          capital=1_000_000, lot_size=50)
        self.assertTrue(d.enter, d.reason)
        self.assertEqual(len(d.legs), 4)
        self.assertGreaterEqual(d.lots, 1)
        self.assertGreater(d.net_credit, 0)
        self.assertGreater(d.max_loss, 0)

    def test_condor_is_defined_risk(self):
        d = self.s.decide(spot=24000, vix=18.0, closes=_flat_closes(daily_vol=0.006),
                          capital=1_000_000, lot_size=50)
        sells = [l for l in d.legs if l.side == Side.SELL]
        buys = [l for l in d.legs if l.side == Side.BUY]
        # exactly 2 short (income) + 2 long wings (protection) = defined risk
        self.assertEqual(len(sells), 2)
        self.assertEqual(len(buys), 2)
        # wings are further OTM than the shorts (real protection)
        call_short = next(l.strike for l in sells if l.option_type == OptionType.CE)
        call_wing = next(l.strike for l in buys if l.option_type == OptionType.CE)
        put_short = next(l.strike for l in sells if l.option_type == OptionType.PE)
        put_wing = next(l.strike for l in buys if l.option_type == OptionType.PE)
        self.assertGreater(call_wing, call_short)
        self.assertLess(put_wing, put_short)
        # max loss can never exceed the wing width (the whole point of defined risk)
        self.assertLessEqual(d.max_loss, self.s.wing_width)

    def test_risk_budget_caps_lots(self):
        small = self.s.decide(spot=24000, vix=16.0, closes=_flat_closes(daily_vol=0.006),
                              capital=100_000, lot_size=50)
        big = self.s.decide(spot=24000, vix=16.0, closes=_flat_closes(daily_vol=0.006),
                            capital=2_000_000, lot_size=50)
        if small.enter and big.enter:
            self.assertLess(small.lots, big.lots)


if __name__ == "__main__":
    unittest.main()
