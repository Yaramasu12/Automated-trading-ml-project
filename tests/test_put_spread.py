"""Bull put spread (downside skew/crash-risk premium) as a second defined-risk
structure alongside the iron condor. Distinct edge: index puts are systematically
overpriced, so selling a defined-risk put spread harvests that skew."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from trading_platform.domain.enums import OptionType, Side
from trading_platform.strategies.short_vol import ShortVolStrategy
from trading_platform.strategies.short_vol_executor import ShortVolExecutor


def _closes(n=40, seed=3):
    return list(24000 * np.exp(np.cumsum(np.random.default_rng(seed).normal(0, 0.006, n))))


class PutSpreadStrategyTests(unittest.TestCase):
    def setUp(self):
        self.s = ShortVolStrategy(sd=1.25, min_vrp=2.0)

    def test_put_spread_is_two_legs_defined_risk(self):
        d = self.s.decide(spot=24000, vix=20.0, closes=_closes(), capital=1_000_000,
                          lot_size=50, forecast_vol=11.0, structure="put_spread")
        self.assertTrue(d.enter, d.reason)
        self.assertEqual(len(d.legs), 2)
        sells = [l for l in d.legs if l.side == Side.SELL]
        buys = [l for l in d.legs if l.side == Side.BUY]
        self.assertEqual(len(sells), 1)
        self.assertEqual(len(buys), 1)
        self.assertTrue(all(l.option_type == OptionType.PE for l in d.legs))
        # protective wing strictly below the short put; loss capped by wing width
        self.assertLess(buys[0].strike, sells[0].strike)
        self.assertLessEqual(d.max_loss, self.s.wing_width)

    def test_one_sided_win_prob_exceeds_two_sided(self):
        # A put spread only loses on a down move, so its win prob > the condor's.
        p_put = self.s.win_probability_one_sided(20.0, 11.0)
        p_condor = self.s.win_probability(20.0, 11.0)
        self.assertGreater(p_put, p_condor)


class StructureConfigTests(unittest.TestCase):
    def _executor(self):
        rt = SimpleNamespace(portfolio=SimpleNamespace(positions={}, cash=1_000_000, equity=1_000_000),
                             instrument_master=SimpleNamespace(expiries=lambda u, seg=None: []))
        return ShortVolExecutor(rt)

    def test_structures_default_condor(self):
        ex = self._executor()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHORTVOL_STRUCTURES", None)
            self.assertEqual(ex.structures, ["condor"])

    def test_structures_parsed_and_validated(self):
        ex = self._executor()
        with mock.patch.dict(os.environ, {"SHORTVOL_STRUCTURES": "condor,put_spread,bogus"}):
            self.assertEqual(ex.structures, ["condor", "put_spread"])


if __name__ == "__main__":
    unittest.main()
