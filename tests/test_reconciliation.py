"""Unit tests for PositionReconciliation — the first-ever test coverage for
this class (confirmed 2026-08-06: it existed, worked for the manual
POST /execution/reconcile path, but had zero tests and a real blind spot —
see test_broker_side_closure_is_detected below).
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from trading_platform.domain.enums import (
    AssetClass, Exchange, InstrumentType, OptionType, Segment,
)
from trading_platform.domain.models import Instrument, Position
from trading_platform.execution.oms_store import OMSEventStore
from trading_platform.execution.reconciliation import PositionReconciliation
from trading_platform.portfolio.ledger import PortfolioLedger


def _option(symbol, strike=24000.0, ot=OptionType.CE):
    return Instrument(
        symbol=symbol, name="NIFTY", exchange=Exchange.NFO, segment=Segment.OPTIONS,
        asset_class=AssetClass.INDEX, instrument_type=InstrumentType.OPTION,
        token="1", lot_size=50, strike=strike, option_type=ot,
        expiry=date(2100, 1, 1), underlying="NIFTY",
    )


class PositionReconciliationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.portfolio = PortfolioLedger(1_000_000.0)
        self.oms = OMSEventStore(db_path=Path(self._tmpdir.name) / "oms.db")
        self.recon = PositionReconciliation(self.portfolio, self.oms)

    def tearDown(self):
        self.oms.close()  # release the SQLite handle first — Windows can't rmtree an open file
        self._tmpdir.cleanup()

    def test_matching_position_has_no_drift(self):
        self.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        results = self.recon.reconcile({"NIFTY24000CE": -50})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].drift, 0)
        self.assertEqual(results[0].action_taken, "none")
        self.assertFalse(self.recon.has_drift({"NIFTY24000CE": -50}))

    def test_quantity_mismatch_is_drift(self):
        self.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        results = self.recon.reconcile({"NIFTY24000CE": -100})
        self.assertEqual(results[0].drift, -50)
        self.assertIn("drift_detected", results[0].action_taken)
        self.assertTrue(self.recon.has_drift({"NIFTY24000CE": -100}))

    def test_broker_reports_unknown_position_is_drift(self):
        """Broker shows a position we have no local record of at all."""
        results = self.recon.reconcile({"NIFTY24000CE": -50})
        self.assertEqual(results[0].local_qty, 0)
        self.assertEqual(results[0].broker_qty, -50)
        self.assertEqual(results[0].drift, -50)

    def test_broker_side_closure_is_detected(self):
        """The real gap this fixes: broker no longer reports a position we
        still think is open (e.g. stopped out or manually closed broker-side)
        — must show up as drift even though it's absent from broker_positions,
        not silently pass because the old code only ever looped over
        broker_positions.items()."""
        self.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        results = self.recon.reconcile({})  # broker reports nothing at all
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].symbol, "NIFTY24000CE")
        self.assertEqual(results[0].broker_qty, 0)
        self.assertEqual(results[0].local_qty, -50)
        self.assertEqual(results[0].drift, 50)
        self.assertTrue(self.recon.has_drift({}))

    def test_zero_quantity_local_position_is_not_treated_as_open(self):
        self.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=0, average_price=150.0
        )
        self.assertFalse(self.recon.has_drift({}))

    def test_both_flat_is_not_drift(self):
        self.assertEqual(self.recon.reconcile({}), [])
        self.assertFalse(self.recon.has_drift({}))

    def test_broker_explicit_zero_matches_no_local_position(self):
        results = self.recon.reconcile({"NIFTY24000CE": 0})
        self.assertEqual(results[0].drift, 0)
        self.assertEqual(results[0].action_taken, "none")

    def test_drift_is_logged_to_oms(self):
        self.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        self.recon.reconcile({"NIFTY24000CE": -100})
        events = self.oms.recent_events(limit=10)
        matching = [e for e in events if e.get("event_type") == "position_reconciled"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["symbol"], "NIFTY24000CE")

    def test_no_drift_does_not_write_oms_event(self):
        self.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        self.recon.reconcile({"NIFTY24000CE": -50})
        self.assertEqual(self.oms.event_count(), 0)

    def test_multiple_symbols_independently_evaluated(self):
        self.portfolio.positions["NIFTY24000CE"] = Position(
            instrument=_option("NIFTY24000CE"), quantity=-50, average_price=150.0
        )
        self.portfolio.positions["NIFTY23000PE"] = Position(
            instrument=_option("NIFTY23000PE", 23000.0, OptionType.PE), quantity=-50, average_price=80.0
        )
        results = self.recon.reconcile({"NIFTY24000CE": -50, "NIFTY23000PE": -100})
        by_symbol = {r.symbol: r for r in results}
        self.assertEqual(by_symbol["NIFTY24000CE"].drift, 0)
        self.assertEqual(by_symbol["NIFTY23000PE"].drift, -50)


if __name__ == "__main__":
    unittest.main()
