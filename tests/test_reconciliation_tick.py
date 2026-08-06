"""Unit tests for TradingRuntime._run_reconciliation_tick — the automated
broker-reconciliation safety net (REDESIGN_PROMPT.md §6.3), mirroring
test_portfolio_guardian.py's pattern (construct a real TradingRuntime, call
the sync tick directly, assert on kill_switch_active)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from trading_platform.api.runtime import TradingRuntime
from trading_platform.domain.enums import (
    AssetClass, Exchange, ExecutionMode, InstrumentType, OptionType, Segment,
)
from trading_platform.domain.models import Instrument, Position


def _option(symbol, strike=24000.0, ot=OptionType.CE):
    from datetime import date
    return Instrument(
        symbol=symbol, name="NIFTY", exchange=Exchange.NFO, segment=Segment.OPTIONS,
        asset_class=AssetClass.INDEX, instrument_type=InstrumentType.OPTION,
        token="1", lot_size=50, strike=strike, option_type=ot,
        expiry=date(2100, 1, 1), underlying="NIFTY",
    )


class ReconciliationTickTests(unittest.TestCase):
    def _runtime(self, broker_positions) -> TradingRuntime:
        rt = TradingRuntime()
        rt.portfolio.positions.clear()
        rt.kill_switch_active = False
        rt._consecutive_reconciliation_mismatches = 0
        rt.execution_mode = ExecutionMode.LIVE
        rt.scheduler.broker = SimpleNamespace(positions=lambda: broker_positions)
        return rt

    def test_noop_outside_live_mode(self):
        rt = self._runtime([{"tradingsymbol": "NIFTY24000CE", "netqty": "-100"}])
        rt.execution_mode = ExecutionMode.PAPER
        rt.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        rt._run_reconciliation_tick()
        self.assertFalse(rt.kill_switch_active)

    def test_matching_positions_no_trip(self):
        rt = self._runtime([{"tradingsymbol": "NIFTY24000CE", "netqty": "-50"}])
        rt.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        rt._run_reconciliation_tick()
        self.assertFalse(rt.kill_switch_active)
        self.assertEqual(rt._consecutive_reconciliation_mismatches, 0)

    def test_single_mismatch_does_not_trip_immediately(self):
        """First sighting could be a same-tick fill-timing race — must not
        trip on tick 1."""
        rt = self._runtime([{"tradingsymbol": "NIFTY24000CE", "netqty": "-100"}])
        rt.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        rt._run_reconciliation_tick()
        self.assertFalse(rt.kill_switch_active)
        self.assertEqual(rt._consecutive_reconciliation_mismatches, 1)

    def test_confirmed_mismatch_across_two_ticks_trips_kill_switch(self):
        rt = self._runtime([{"tradingsymbol": "NIFTY24000CE", "netqty": "-100"}])
        rt.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        rt._run_reconciliation_tick()
        rt._run_reconciliation_tick()
        self.assertTrue(rt.kill_switch_active)

    def test_mismatch_resolving_before_second_tick_resets_counter(self):
        rt = self._runtime([{"tradingsymbol": "NIFTY24000CE", "netqty": "-100"}])
        rt.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        rt._run_reconciliation_tick()
        self.assertEqual(rt._consecutive_reconciliation_mismatches, 1)
        # Broker position now matches (e.g. our own fill just caught up)
        rt.scheduler.broker = SimpleNamespace(
            positions=lambda: [{"tradingsymbol": "NIFTY24000CE", "netqty": "-50"}]
        )
        rt._run_reconciliation_tick()
        self.assertFalse(rt.kill_switch_active)
        self.assertEqual(rt._consecutive_reconciliation_mismatches, 0)

    def test_broker_side_closure_trips_after_two_ticks(self):
        """The real gap fixed in PositionReconciliation: broker no longer
        reports a position we still think is open."""
        rt = self._runtime([])
        rt.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        rt._run_reconciliation_tick()
        rt._run_reconciliation_tick()
        self.assertTrue(rt.kill_switch_active)

    def test_already_tripped_kill_switch_short_circuits(self):
        rt = self._runtime([{"tradingsymbol": "NIFTY24000CE", "netqty": "-999"}])
        rt.kill_switch_active = True
        rt.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        rt._run_reconciliation_tick()  # must not raise; early return
        self.assertTrue(rt.kill_switch_active)

    def test_broker_fetch_failure_does_not_raise_or_trip(self):
        rt = self._runtime([])

        def boom():
            raise RuntimeError("broker API down")
        rt.scheduler.broker = SimpleNamespace(positions=boom)
        rt._run_reconciliation_tick()  # must not raise
        self.assertFalse(rt.kill_switch_active)

    def test_healthy_book_across_repeated_ticks_stays_clear(self):
        rt = self._runtime([{"tradingsymbol": "NIFTY24000CE", "netqty": "-50"}])
        rt.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        for _ in range(5):
            rt._run_reconciliation_tick()
        self.assertFalse(rt.kill_switch_active)


if __name__ == "__main__":
    unittest.main()
