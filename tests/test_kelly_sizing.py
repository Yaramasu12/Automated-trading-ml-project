"""Kelly sizing for the short-vol condor: position size scales with the REAL
edge (win prob under the forecast-vol distribution), always capped by the
risk budget so it can never over-bet a fat-tailed short-vol payoff."""
from __future__ import annotations

import unittest

import numpy as np

from trading_platform.strategies.short_vol import ShortVolStrategy


def _closes(n=40, seed=1):
    r = np.random.default_rng(seed).normal(0, 0.006, n)
    return list(24000 * np.exp(np.cumsum(r)))


class KellySizingTests(unittest.TestCase):
    def setUp(self):
        self.s = ShortVolStrategy(sd=1.25, min_vrp=2.0)

    def test_win_prob_rises_as_forecast_falls_below_implied(self):
        p_hi = self.s.win_probability(16.0, 9.0)    # big VRP
        p_mid = self.s.win_probability(16.0, 12.0)
        p_lo = self.s.win_probability(16.0, 16.0)   # no VRP
        self.assertGreater(p_hi, p_mid)
        self.assertGreater(p_mid, p_lo)
        self.assertTrue(0.5 < p_lo < 0.85)

    def test_kelly_never_exceeds_risk_cap(self):
        # Even with a near-certain win prob, lots are capped by the risk budget.
        lots = self.s.kelly_lots(credit=75, max_loss=225, win_prob=0.99,
                                 capital=1_000_000, lot_size=50, risk_cap_lots=3)
        self.assertLessEqual(lots, 3)

    def test_kelly_zero_when_no_edge(self):
        # p_win low enough that Kelly f* <= 0 -> don't trade.
        lots = self.s.kelly_lots(credit=50, max_loss=250, win_prob=0.55,
                                 capital=1_000_000, lot_size=50, risk_cap_lots=10)
        self.assertEqual(lots, 0)

    def test_bigger_edge_sizes_at_least_as_large(self):
        rich = self.s.decide(spot=24000, vix=22.0, closes=_closes(), capital=5_000_000,
                             lot_size=50, forecast_vol=9.0)
        thin = self.s.decide(spot=24000, vix=16.5, closes=_closes(), capital=5_000_000,
                             lot_size=50, forecast_vol=14.0)
        if rich.enter and thin.enter:
            self.assertGreaterEqual(rich.lots, thin.lots)

    def test_kelly_disabled_falls_back_to_risk_budget(self):
        s = ShortVolStrategy(sd=1.25, min_vrp=2.0, kelly_fraction=0.0)
        d = s.decide(spot=24000, vix=20.0, closes=_closes(), capital=1_000_000,
                     lot_size=50, forecast_vol=10.0)
        if d.enter:
            self.assertIn("risk_budget", d.reason)


if __name__ == "__main__":
    unittest.main()
