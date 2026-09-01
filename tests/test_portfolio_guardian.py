"""Unit tests for the always-on Portfolio Guardian safety net, added
2026-08-05 after a short-vol condor breached its stop-loss by over 3x with
nothing watching for hours (see runtime.py's
_periodic_portfolio_guardian_loop docstring for the full incident)."""
from __future__ import annotations

import unittest
from datetime import date

from trading_platform.api.runtime import TradingRuntime
from trading_platform.domain.enums import (
    AssetClass, Exchange, InstrumentType, OptionType, Segment,
)
from trading_platform.domain.models import Instrument, Position


def _option(symbol, strike=24000.0, ot=OptionType.CE, lot_size=50):
    return Instrument(
        symbol=symbol, name="NIFTY", exchange=Exchange.NFO, segment=Segment.OPTIONS,
        asset_class=AssetClass.INDEX, instrument_type=InstrumentType.OPTION,
        token="1", lot_size=lot_size, strike=strike, option_type=ot,
        expiry=date(2100, 1, 1), underlying="NIFTY",
    )


class PortfolioGuardianTests(unittest.TestCase):
    def _runtime(self) -> TradingRuntime:
        rt = TradingRuntime()
        rt.portfolio.positions.clear()
        rt.kill_switch_active = False
        return rt

    def test_no_breach_does_not_set_kill_switch(self):
        rt = self._runtime()
        rt.portfolio.cash = 100_000.0
        rt.portfolio.peak_equity = 100_000.0
        rt._run_portfolio_guardian_tick()
        self.assertFalse(rt.kill_switch_active)

    def test_drawdown_breach_sets_kill_switch(self):
        rt = self._runtime()
        rt.portfolio.peak_equity = 100_000.0
        rt.portfolio.cash = 100_000.0 * (1 - rt.capital_protection.drawdown_halt_pct - 0.01)
        rt._run_portfolio_guardian_tick()
        self.assertTrue(rt.kill_switch_active)

    def test_daily_loss_breach_sets_kill_switch(self):
        rt = self._runtime()
        rt.portfolio.peak_equity = 1_000_000.0
        rt.portfolio.cash = 1_000_000.0 * (1 - rt.capital_protection.daily_loss_limit_pct - 0.005)
        rt.scheduler._session_start_equity = 1_000_000.0
        rt._run_portfolio_guardian_tick()
        self.assertTrue(rt.kill_switch_active)

    def test_already_tripped_kill_switch_short_circuits(self):
        rt = self._runtime()
        rt.kill_switch_active = True
        rt.portfolio.cash = 1.0
        rt.portfolio.peak_equity = 1_000_000.0
        rt._run_portfolio_guardian_tick()   # must not raise; early return
        self.assertTrue(rt.kill_switch_active)

    def test_healthy_portfolio_across_repeated_ticks_stays_clear(self):
        rt = self._runtime()
        rt.portfolio.cash = 100_000.0
        rt.portfolio.peak_equity = 100_000.0
        for _ in range(5):
            rt._run_portfolio_guardian_tick()
        self.assertFalse(rt.kill_switch_active)

    def test_restart_with_open_short_position_does_not_false_trip(self):
        """2026-09-01: PortfolioLedger.equity (the cost-basis property used to
        seed session_start_equity on every restart, runtime.py's
        restore_state -> set_session_start_equity) summed abs(quantity) for
        every position, double-counting a SHORT position's premium (already
        credited to cash at entry) as if it were held long. On a real PAPER
        book with short condor legs this inflated the restart-time baseline
        ~2.1% above the very next mark-to-market read, tripping the daily-loss
        breaker with zero actual price movement. Reproduces that restart
        sequence directly: restore a short position, seed session_start from
        the property exactly as runtime.py does, then tick with nothing moved."""
        rt = self._runtime()
        instrument = _option("BANKNIFTY29SEP2661600CE", lot_size=35)
        rt.portfolio.cash = 100_000.0 + 130.47 * 35  # premium credited at entry
        rt.portfolio.positions["BANKNIFTY29SEP2661600CE"] = Position(
            instrument=instrument, quantity=-1, average_price=130.47,
        )
        rt.portfolio.peak_equity = rt.portfolio.equity
        rt.scheduler.set_session_start_equity(rt.portfolio.equity)  # mirrors restore_state()
        rt._run_portfolio_guardian_tick()  # nothing has moved: must not breach
        self.assertFalse(rt.kill_switch_active)


class PortfolioEquityShortPositionTests(unittest.TestCase):
    """Direct coverage of the PortfolioLedger.equity cost-basis property —
    see the false-trip regression test above for the end-to-end scenario
    this property feeds into."""

    def test_short_position_does_not_inflate_cost_basis_equity(self):
        rt = TradingRuntime()
        rt.portfolio.positions.clear()
        instrument = _option("BANKNIFTY29SEP2661600CE", lot_size=35)
        premium_received = 130.47 * 35
        rt.portfolio.cash = 100_000.0 + premium_received
        rt.portfolio.positions["BANKNIFTY29SEP2661600CE"] = Position(
            instrument=instrument, quantity=-1, average_price=130.47,
        )
        # A short position's cost-basis equity must equal cash before the
        # trade (premium in cash exactly offsets the liability at cost) --
        # not cash + a second copy of the same premium.
        self.assertAlmostEqual(rt.portfolio.equity, 100_000.0, places=6)

    def test_long_position_cost_basis_unaffected(self):
        rt = TradingRuntime()
        rt.portfolio.positions.clear()
        instrument = _option("BANKNIFTY29SEP2661600PE", lot_size=35)
        cost_paid = 25.0 * 35
        rt.portfolio.cash = 100_000.0 - cost_paid
        rt.portfolio.positions["BANKNIFTY29SEP2661600PE"] = Position(
            instrument=instrument, quantity=1, average_price=25.0,
        )
        self.assertAlmostEqual(rt.portfolio.equity, 100_000.0, places=6)


if __name__ == "__main__":
    unittest.main()
