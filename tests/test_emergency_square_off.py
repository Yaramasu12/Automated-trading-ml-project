"""Tests for EmergencySquareOff (2026-08-03):
- closing product_type must match the position's own type (options -> CARRYFORWARD,
  everything else -> INTRADAY) — closing a CARRYFORWARD position with an INTRADAY
  exit does not net out at the broker (see broker/angel_one.py's producttype mapping).
- an explicitly empty `symbols` allow-list must close nothing, not silently fall
  back to no-filter/GLOBAL (a truthiness bug: `set(symbols) if symbols else None`
  treated [] the same as "no filter given").
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date
from types import SimpleNamespace

from trading_platform.domain.enums import (
    AssetClass, Exchange, InstrumentType, OptionType, ProductType, Segment, SquareOffScope,
)
from trading_platform.domain.models import Instrument, Position
from trading_platform.execution.emergency_square_off import EmergencySquareOff


def _option_inst(symbol="NIFTY24000CE", strike=24000.0):
    return Instrument(
        symbol=symbol, name="NIFTY", exchange=Exchange.NFO,
        segment=Segment.OPTIONS, asset_class=AssetClass.INDEX,
        instrument_type=InstrumentType.OPTION, token=symbol, lot_size=50, tick_size=0.05,
        expiry=date(2100, 1, 7), strike=strike, option_type=OptionType.CE, underlying="NIFTY",
    )


def _equity_inst(symbol="RELIANCE"):
    return Instrument(
        symbol=symbol, name=symbol, exchange=Exchange.NSE,
        segment=Segment.CASH, asset_class=AssetClass.EQUITY,
        instrument_type=InstrumentType.EQUITY, token="1",
    )


class EmergencySquareOffProductTypeTests(unittest.TestCase):
    def _square_off(self, positions, mark_price=100.0):
        submitted = []

        async def enqueue(intent):
            submitted.append(intent)
            return intent.idempotency_key

        portfolio = SimpleNamespace(positions=positions)
        eso = EmergencySquareOff(portfolio, enqueue, mark_source=lambda s: mark_price)
        return eso, submitted

    def test_closing_options_position_uses_carryforward(self):
        positions = {"NIFTY24000CE": Position(instrument=_option_inst(), quantity=-1, average_price=100.0)}
        eso, submitted = self._square_off(positions)
        asyncio.run(eso.square_off(scope=SquareOffScope.GLOBAL, reason="test"))
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].product_type, ProductType.CARRYFORWARD)

    def test_closing_equity_position_stays_intraday(self):
        positions = {"RELIANCE": Position(instrument=_equity_inst(), quantity=10, average_price=2800.0)}
        eso, submitted = self._square_off(positions)
        asyncio.run(eso.square_off(scope=SquareOffScope.GLOBAL, reason="test"))
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].product_type, ProductType.INTRADAY)

    def test_mixed_positions_get_matching_product_types(self):
        positions = {
            "NIFTY24000CE": Position(instrument=_option_inst(), quantity=-1, average_price=100.0),
            "RELIANCE": Position(instrument=_equity_inst(), quantity=10, average_price=2800.0),
        }
        eso, submitted = self._square_off(positions)
        asyncio.run(eso.square_off(scope=SquareOffScope.GLOBAL, reason="test"))
        by_symbol = {i.instrument.symbol: i for i in submitted}
        self.assertEqual(by_symbol["NIFTY24000CE"].product_type, ProductType.CARRYFORWARD)
        self.assertEqual(by_symbol["RELIANCE"].product_type, ProductType.INTRADAY)

    def test_empty_symbols_allowlist_closes_nothing(self):
        """Regression: symbols=[] must NOT be treated the same as symbols=None
        (no filter) — that would silently close everything, the opposite of
        what an exclude-everything-that-matched allow-list means."""
        positions = {"RELIANCE": Position(instrument=_equity_inst(), quantity=10, average_price=2800.0)}
        eso, submitted = self._square_off(positions)
        result = asyncio.run(eso.square_off(scope=SquareOffScope.GLOBAL, symbols=[], reason="test"))
        self.assertEqual(len(submitted), 0)
        self.assertEqual(result["positions_targeted"], 0)

    def test_none_symbols_means_no_filter(self):
        positions = {"RELIANCE": Position(instrument=_equity_inst(), quantity=10, average_price=2800.0)}
        eso, submitted = self._square_off(positions)
        asyncio.run(eso.square_off(scope=SquareOffScope.GLOBAL, symbols=None, reason="test"))
        self.assertEqual(len(submitted), 1)


if __name__ == "__main__":
    unittest.main()
