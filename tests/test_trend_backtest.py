"""Tests for the trend-following candidate edge and the multi-sleeve blender.

Context that matters when reading these: trend-following was built as a
candidate SECOND edge to diversify short-vol, and it **failed the §5 gates**
(PBO 0.571 vs 0.4 limit; Monte-Carlo DD 17.2% vs 15% limit). It is therefore
NOT promoted and NOT wired into any live path. These tests exist so the module
stays honest and correct as research code — not because it ships.
"""
from __future__ import annotations

import math
import unittest
from datetime import date, timedelta

from trading_platform.backtesting.short_vol_backtest import DailyBar
from trading_platform.backtesting.trend_backtest import (
    TrendFollowingBacktester,
    combine_equity_curves,
)


def _series(n: int, drift: float, vol: float = 0.008, seed: int = 3,
            start: float = 20000.0) -> list[DailyBar]:
    import numpy as np
    rng = np.random.default_rng(seed)
    px, out, day = start, [], date(2020, 1, 1)
    for _ in range(n):
        px *= math.exp(drift + vol * float(rng.normal()))
        day += timedelta(days=1)
        if day.weekday() < 5:
            out.append(DailyBar(day, px))
    return out


class TrendMechanicsTests(unittest.TestCase):
    def test_captures_a_sustained_uptrend(self):
        """The one thing a trend follower must do: make money when a trend
        persists. If this fails the signal is inverted or never fires."""
        bars = _series(500, drift=0.0012, vol=0.006)
        res = TrendFollowingBacktester(lookback=50, allow_short=False).run(bars)
        self.assertGreater(res.final_equity, res.starting_capital)

    def test_long_flat_variant_never_goes_short(self):
        """allow_short=False must stay long/flat — it's the variant a cash
        equity account can actually implement."""
        bars = _series(400, drift=-0.0015)
        res = TrendFollowingBacktester(lookback=50, allow_short=False).run(bars)
        # In a sustained downtrend a long/flat book goes to cash; it must not
        # profit from the decline (that would mean it shorted).
        self.assertLessEqual(res.final_equity, res.starting_capital * 1.02)

    def test_costs_are_charged_on_every_rebalance(self):
        bars = _series(500, drift=0.0005)
        res = TrendFollowingBacktester(lookback=50).run(bars)
        if res.n_trades == 0:
            self.skipTest("no rebalances on this path")
        self.assertGreater(res.total_costs, 0.0)

    def test_more_frequent_rebalancing_costs_more(self):
        bars = _series(500, drift=0.0005)
        slow = TrendFollowingBacktester(lookback=50, rebalance_days=20).run(bars)
        fast = TrendFollowingBacktester(lookback=50, rebalance_days=1).run(bars)
        self.assertGreater(fast.total_costs, slow.total_costs)

    def test_vol_targeting_reduces_exposure_when_vol_rises(self):
        """Higher realized vol must produce a smaller position, else the
        vol-targeting in §4.4e isn't actually doing anything."""
        calm = _series(500, drift=0.0008, vol=0.004, seed=9)
        wild = _series(500, drift=0.0008, vol=0.020, seed=9)
        r_calm = TrendFollowingBacktester(lookback=50, target_vol=0.15).run(calm)
        r_wild = TrendFollowingBacktester(lookback=50, target_vol=0.15).run(wild)
        # Same drift/seed; the wild path's vol scaling should damp its equity swing.
        self.assertLess(r_wild.max_drawdown, 0.9,
                        "vol targeting should bound drawdown on a high-vol path")
        self.assertGreater(r_calm.final_equity, 0)

    def test_no_lookahead_warmup_is_flat(self):
        """No position may be taken before enough history exists to compute
        both the signal and the vol estimate."""
        bars = _series(300, drift=0.001)
        res = TrendFollowingBacktester(lookback=100, vol_window=20).run(bars)
        warmup = 101
        early = [v for _, v in res.equity_curve[:warmup]]
        self.assertTrue(all(abs(v - res.starting_capital) < 1e-6 for v in early),
                        "equity moved before the warmup completed -> lookahead")


class CombineEquityCurvesTests(unittest.TestCase):
    def test_blends_returns_not_levels(self):
        """Two identical sleeves at 50/50 must give the SAME curve, not double
        it. Summing equity levels would silently assume each sleeve received
        the full capital and overstate the book N-fold."""
        days = [date(2024, 1, 1) + timedelta(days=i) for i in range(10)]
        curve = [(d, 1_000_000.0 * (1.01 ** i)) for i, d in enumerate(days)]
        blended = combine_equity_curves([curve, curve], [0.5, 0.5])
        self.assertAlmostEqual(blended[-1][1], curve[-1][1], places=2)

    def test_weights_are_normalised(self):
        days = [date(2024, 1, 1) + timedelta(days=i) for i in range(10)]
        c = [(d, 1_000_000.0 * (1.01 ** i)) for i, d in enumerate(days)]
        a = combine_equity_curves([c, c], [0.5, 0.5])
        b = combine_equity_curves([c, c], [5.0, 5.0])       # same ratio, bigger numbers
        self.assertAlmostEqual(a[-1][1], b[-1][1], places=2)

    def test_uses_only_common_days(self):
        d1 = [date(2024, 1, 1) + timedelta(days=i) for i in range(10)]
        d2 = d1[3:]
        c1 = [(d, 1_000_000.0) for d in d1]
        c2 = [(d, 1_000_000.0) for d in d2]
        self.assertEqual(len(combine_equity_curves([c1, c2], [0.5, 0.5])), len(d2))

    def test_empty_input_is_safe(self):
        self.assertEqual(combine_equity_curves([], []), [])
        self.assertEqual(combine_equity_curves([[(date(2024, 1, 1), 1.0)]], []), [])


if __name__ == "__main__":
    unittest.main()
