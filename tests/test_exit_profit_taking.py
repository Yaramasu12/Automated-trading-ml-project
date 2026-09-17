"""Realistic intraday profit-taking: exit targets must be tight enough that a
normal index/futures intraday move actually books the winner during the session,
instead of a 6% ATR target that never triggers and gets dumped at EOD."""
from __future__ import annotations

import unittest
from datetime import datetime

from trading_platform.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentType,
    Segment,
    Side,
)
from trading_platform.domain.models import Instrument, Trade
from trading_platform.exit.exit_plan import ExitPlan, ExitTrigger


def _future(symbol="NIFTY24000FUT", underlying="NIFTY"):
    return Instrument(
        symbol=symbol, name=underlying, exchange=Exchange.NFO, segment=Segment.FUTURES,
        asset_class=AssetClass.INDEX, instrument_type=InstrumentType.FUTURE, token="1",
        lot_size=50, tick_size=0.05, expiry=None, strike=None, option_type=None,
        underlying=underlying,
    )


def _capped_plan(side, entry, atr, tgt_pct=0.008, stop_pct=0.005):
    """Build an ExitPlan the way _on_fill does, then apply the intraday caps."""
    tr = Trade("t", "o", "NIFTY24000FUT", side, 1, entry, 0.0, datetime(2026, 7, 20, 10, 0), "agent")
    plan = ExitPlan.from_trade(tr, instrument=_future(), atr=atr, atr_stop_multiplier=2.0, partial_exit=True)
    ep = plan.entry_price
    tgt, stp = ep * tgt_pct, ep * stop_pct
    if plan.side == "BUY":
        plan.target_price = min(plan.target_price or (ep + tgt), ep + tgt)
        plan.stop_loss_price = max(plan.stop_loss_price or (ep - stp), ep - stp)
    else:
        plan.target_price = max(plan.target_price or (ep - tgt), ep - tgt)
        plan.stop_loss_price = min(plan.stop_loss_price or (ep + stp), ep + stp)
    return plan


class ExitProfitTakingTests(unittest.TestCase):
    def test_wide_atr_target_is_capped_to_intraday(self):
        # ATR 300 on a 24000 index -> raw target ~6% (never hits intraday).
        plan = _capped_plan(Side.BUY, 24000.0, atr=300.0)
        self.assertLessEqual(plan.target_price, 24000 * 1.0081)   # capped ~0.8%
        self.assertGreaterEqual(plan.stop_loss_price, 24000 * 0.9949)  # ~0.5%

    def test_realistic_up_move_books_profit_long(self):
        plan = _capped_plan(Side.BUY, 24000.0, atr=300.0)
        trig = plan.check_trigger(24000 * 1.0085, datetime(2026, 7, 20, 11, 0))
        self.assertIn(trig, (ExitTrigger.TARGET, ExitTrigger.PARTIAL_TARGET))

    def test_realistic_down_move_books_profit_short(self):
        plan = _capped_plan(Side.SELL, 24000.0, atr=300.0)
        trig = plan.check_trigger(24000 * 0.9915, datetime(2026, 7, 20, 11, 0))
        self.assertIn(trig, (ExitTrigger.TARGET, ExitTrigger.PARTIAL_TARGET))

    def test_stop_cuts_loss_long(self):
        plan = _capped_plan(Side.BUY, 24000.0, atr=300.0)
        trig = plan.check_trigger(24000 * 0.994, datetime(2026, 7, 20, 11, 0))
        self.assertEqual(trig, ExitTrigger.STOP_LOSS)


class MaxAdverseExcursionTests(unittest.TestCase):
    """Section 12 audit (2026-09-17): _highest_price/_lowest_price only
    updated when a trailing stop was configured, so max_adverse_excursion/
    max_favorable_excursion would have stayed frozen at zero for the common
    case (no trailing_pct) -- the same silently-fake-number pattern found
    elsewhere this session. These prove the price extremes now track on
    every mark regardless of trailing, and MAE/MFE compute real, correctly
    signed rupee values from them."""

    def test_long_tracks_extremes_without_trailing_configured(self):
        tr = Trade("t", "o", "NIFTY24000FUT", Side.BUY, 1, 24000.0, 0.0,
                    datetime(2026, 7, 20, 10, 0), "agent")
        plan = ExitPlan.from_trade(tr, instrument=_future())  # no trailing_pct
        self.assertIsNone(plan.trailing_pct)

        plan.check_trigger(23800.0, datetime(2026, 7, 20, 10, 5))   # dips first
        plan.check_trigger(24300.0, datetime(2026, 7, 20, 10, 10))  # then rallies
        plan.check_trigger(24100.0, datetime(2026, 7, 20, 10, 15))  # settles

        lot_size = _future().lot_size
        self.assertAlmostEqual(plan.max_adverse_excursion(), 200.0 * lot_size)
        self.assertAlmostEqual(plan.max_favorable_excursion(), 300.0 * lot_size)

    def test_short_side_has_inverted_adverse_and_favorable_directions(self):
        tr = Trade("t", "o", "NIFTY24000FUT", Side.SELL, 1, 24000.0, 0.0,
                    datetime(2026, 7, 20, 10, 0), "agent")
        plan = ExitPlan.from_trade(tr, instrument=_future())

        plan.check_trigger(24250.0, datetime(2026, 7, 20, 10, 5))   # adverse: price up
        plan.check_trigger(23700.0, datetime(2026, 7, 20, 10, 10))  # favorable: price down

        lot_size = _future().lot_size
        self.assertAlmostEqual(plan.max_adverse_excursion(), 250.0 * lot_size)
        self.assertAlmostEqual(plan.max_favorable_excursion(), 300.0 * lot_size)

    def test_no_adverse_move_reports_zero_not_a_fabricated_number(self):
        """A trade that only ever moved in its favor has a real MAE of 0 --
        distinct from the old always-0.0 default, which meant this exact
        same number regardless of what actually happened."""
        tr = Trade("t", "o", "NIFTY24000FUT", Side.BUY, 1, 24000.0, 0.0,
                    datetime(2026, 7, 20, 10, 0), "agent")
        plan = ExitPlan.from_trade(tr, instrument=_future())
        plan.check_trigger(24500.0, datetime(2026, 7, 20, 10, 5))
        self.assertEqual(plan.max_adverse_excursion(), 0.0)
        self.assertGreater(plan.max_favorable_excursion(), 0.0)

    def test_to_dict_exposes_both_excursions(self):
        tr = Trade("t", "o", "NIFTY24000FUT", Side.BUY, 1, 24000.0, 0.0,
                    datetime(2026, 7, 20, 10, 0), "agent")
        plan = ExitPlan.from_trade(tr, instrument=_future())
        plan.check_trigger(23900.0, datetime(2026, 7, 20, 10, 5))

        d = plan.to_dict()
        self.assertIn("max_adverse_excursion", d)
        self.assertIn("max_favorable_excursion", d)
        self.assertGreater(d["max_adverse_excursion"], 0.0)

    def test_trailing_stop_still_updates_correctly_alongside_extremes(self):
        """Regression guard: separating extremes-tracking from trailing-stop
        -raising must not change trailing behaviour when trailing IS
        configured."""
        tr = Trade("t", "o", "NIFTY24000FUT", Side.BUY, 1, 24000.0, 0.0,
                    datetime(2026, 7, 20, 10, 0), "agent")
        plan = ExitPlan.from_trade(tr, instrument=_future(), trailing_pct=0.01)
        plan.check_trigger(24500.0, datetime(2026, 7, 20, 10, 5))
        self.assertAlmostEqual(plan.stop_loss_price, 24500.0 * 0.99, places=4)


if __name__ == "__main__":
    unittest.main()
