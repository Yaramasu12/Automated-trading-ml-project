"""Unit tests for the defined-risk short-vol strategy logic."""
from __future__ import annotations

import asyncio
import math
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import numpy as np

from trading_platform.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentType,
    OptionType,
    Segment,
    Side,
)
from trading_platform.domain.models import Instrument, Position
from trading_platform.strategies.short_vol import ShortVolStrategy
from trading_platform.strategies.short_vol_executor import ShortVolExecutor


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


def _option(underlying="NIFTY", strike=24000.0, ot=OptionType.CE):
    return Instrument(
        symbol=f"{underlying}{int(strike)}{ot.value}", name=underlying,
        exchange=Exchange.NFO,
        segment=Segment.OPTIONS, asset_class=AssetClass.INDEX,
        instrument_type=InstrumentType.OPTION, token="1", lot_size=50, tick_size=0.05,
        expiry=None, strike=strike, option_type=ot, underlying=underlying,
    )


class ShortVolAutoEntryTests(unittest.TestCase):
    def _executor(self, positions):
        rt = SimpleNamespace(portfolio=SimpleNamespace(positions=positions))
        return ShortVolExecutor(rt)

    def test_has_open_condor_true_when_option_position_open(self):
        pos = {"NIFTY24000CE": Position(instrument=_option(), quantity=-50)}
        ex = self._executor(pos)
        self.assertTrue(ex.has_open_condor("NIFTY"))
        self.assertFalse(ex.has_open_condor("BANKNIFTY"))

    def test_has_open_condor_false_when_flat(self):
        pos = {"NIFTY24000CE": Position(instrument=_option(), quantity=0)}
        self.assertFalse(self._executor(pos).has_open_condor("NIFTY"))

    def test_is_entry_window(self):
        ex = self._executor({})
        with mock.patch.dict(os.environ, {"SHORTVOL_ENTRY_WEEKDAY": "0", "SHORTVOL_ENTRY_HOUR": "10"}):
            self.assertTrue(ex.is_entry_window(datetime(2026, 7, 13, 10, 30)))   # Monday 10:30
            self.assertFalse(ex.is_entry_window(datetime(2026, 7, 13, 9, 30)))   # Monday 09:30 (too early)
            self.assertFalse(ex.is_entry_window(datetime(2026, 7, 14, 10, 30)))  # Tuesday

    def test_auto_enter_disabled_by_default(self):
        ex = self._executor({})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHORTVOL_AUTO_ENABLED", None)
            out = asyncio.run(ex.auto_enter(datetime(2026, 7, 13, 10, 30)))
        self.assertFalse(out["ran"])

    def test_auto_enter_skips_when_condor_open(self):
        pos = {"NIFTY24000CE": Position(instrument=_option(), quantity=-50)}
        ex = self._executor(pos)
        with mock.patch.dict(os.environ, {"SHORTVOL_AUTO_ENABLED": "true", "SHORTVOL_AUTO_UNDERLYINGS": "NIFTY"}):
            out = asyncio.run(ex.auto_enter(datetime(2026, 7, 13, 10, 30)))
        self.assertTrue(out["ran"])
        self.assertEqual(out["results"][0]["reason"], "condor already open")
        self.assertFalse(out["results"][0]["submitted"])


class CondorExitContractTests(unittest.TestCase):
    """Locks in the invariant: a defined-risk condor leg is held to expiry —
    a premium swing must NOT stop it out (that would unbalance the structure)."""

    def _expiry_only_plan(self, side=Side.SELL, expiry_days=3):
        from datetime import date as _date, timedelta as _td
        from trading_platform.domain.models import Trade
        from trading_platform.exit.exit_plan import ExitPlan
        trade = Trade(
            trade_id="t1", order_id="o1", symbol="NIFTY24000CE", side=side,
            quantity=50, price=100.0, charges=0.0, timestamp=datetime(2026, 7, 13, 10, 0),
            strategy_name="short_vol_condor",
        )
        plan = ExitPlan.from_trade(trade, instrument=_option(ot=OptionType.CE),
                                   expiry_date=_date.today() + _td(days=expiry_days))
        # Same nulling on_fill applies to multi-leg condor legs:
        plan.stop_loss_price = None
        plan.target_price = None
        plan.trailing_pct = None
        plan.partial_exit_enabled = False
        return plan

    def test_premium_swing_does_not_trigger(self):
        from trading_platform.exit.exit_plan import ExitTrigger
        plan = self._expiry_only_plan()
        # Short premium doubling (a big adverse move) must NOT exit the leg.
        self.assertIsNone(plan.check_trigger(200.0, datetime(2026, 7, 14, 11, 0)))
        # Premium collapsing to near zero (a big favourable move) also holds.
        self.assertIsNone(plan.check_trigger(5.0, datetime(2026, 7, 14, 11, 0)))

    def test_exits_at_expiry(self):
        from datetime import date as _date, timedelta as _td
        from trading_platform.exit.exit_plan import ExitTrigger
        plan = self._expiry_only_plan(expiry_days=0)
        trig = plan.check_trigger(120.0, datetime.combine(_date.today() + _td(days=0), datetime.min.time()))
        self.assertEqual(trig, ExitTrigger.EXPIRY)


if __name__ == "__main__":
    unittest.main()
