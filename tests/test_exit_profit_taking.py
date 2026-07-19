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


if __name__ == "__main__":
    unittest.main()
