"""Regression 2026-08-03: the daily EOD equity square-off must not touch
options (short-vol condor) positions — they're held for days on their own
expiry-based ExitPlan, not squared off intraday. Only true intraday
equity/futures positions belong in the sweep; commodities are excluded too
(handled by the separate MCX EOD job)."""
from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace

from trading_platform.agent.trading_agent import TradingAgent
from trading_platform.domain.enums import AssetClass, Exchange, InstrumentType, OptionType, Segment
from trading_platform.domain.models import Instrument, Position


def _equity_inst(symbol):
    return Instrument(symbol=symbol, name=symbol, exchange=Exchange.NSE, segment=Segment.CASH,
                       asset_class=AssetClass.EQUITY, instrument_type=InstrumentType.EQUITY, token="1")


def _option_inst(symbol="NIFTY24000CE"):
    return Instrument(symbol=symbol, name="NIFTY", exchange=Exchange.NFO, segment=Segment.OPTIONS,
                       asset_class=AssetClass.INDEX, instrument_type=InstrumentType.OPTION, token=symbol,
                       lot_size=50, expiry=date(2100, 1, 7), strike=24000.0, option_type=OptionType.CE,
                       underlying="NIFTY")


def _commodity_inst(symbol="GOLD"):
    return Instrument(symbol=symbol, name=symbol, exchange=Exchange.MCX, segment=Segment.FUTURES,
                       asset_class=AssetClass.COMMODITY, instrument_type=InstrumentType.FUTURE, token=symbol)


class EodSquareOffFilterTests(unittest.TestCase):
    def _agent(self, positions):
        runtime = SimpleNamespace(portfolio=SimpleNamespace(
            position_symbols=lambda: [s for s, p in positions.items() if p.quantity != 0],
            positions=positions,
        ))
        return TradingAgent(runtime)

    def test_equity_position_included(self):
        positions = {"RELIANCE": Position(instrument=_equity_inst("RELIANCE"), quantity=10, average_price=2800.0)}
        agent = self._agent(positions)
        self.assertEqual(agent._intraday_equity_positions(), ["RELIANCE"])

    def test_options_position_excluded(self):
        positions = {"NIFTY24000CE": Position(instrument=_option_inst(), quantity=-1, average_price=100.0)}
        agent = self._agent(positions)
        self.assertEqual(agent._intraday_equity_positions(), [])

    def test_commodity_position_excluded(self):
        positions = {"GOLD": Position(instrument=_commodity_inst(), quantity=1, average_price=90000.0)}
        agent = self._agent(positions)
        self.assertEqual(agent._intraday_equity_positions(), [])

    def test_mixed_positions_only_equity_survives(self):
        positions = {
            "RELIANCE": Position(instrument=_equity_inst("RELIANCE"), quantity=10, average_price=2800.0),
            "NIFTY24000CE": Position(instrument=_option_inst(), quantity=-1, average_price=100.0),
            "GOLD": Position(instrument=_commodity_inst(), quantity=1, average_price=90000.0),
            "FLAT": Position(instrument=_equity_inst("FLAT"), quantity=0, average_price=10.0),
        }
        agent = self._agent(positions)
        self.assertEqual(agent._intraday_equity_positions(), ["RELIANCE"])


if __name__ == "__main__":
    unittest.main()
