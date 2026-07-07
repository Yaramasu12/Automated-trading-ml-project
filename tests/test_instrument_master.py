from __future__ import annotations

import unittest
from datetime import date

from trading_platform.data.instrument_master import InstrumentMaster, build_default_universe
from trading_platform.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentType,
    OptionType,
    Segment,
)
from trading_platform.domain.models import Instrument


class InstrumentMasterTests(unittest.TestCase):
    def test_builds_expiry_aware_index_derivatives(self):
        master = build_default_universe(date(2026, 1, 5))
        future = master.select_future("NIFTY", date(2026, 1, 5))
        option = master.select_option("BANKNIFTY", date(2026, 1, 5), 48520, OptionType.CE)

        self.assertEqual(future.segment, Segment.FUTURES)
        self.assertEqual(future.underlying, "NIFTY")
        self.assertEqual(option.segment, Segment.OPTIONS)
        self.assertEqual(option.option_type, OptionType.CE)
        self.assertGreaterEqual(option.expiry, date(2026, 1, 5))

    def test_includes_equity_cash_symbols(self):
        master = build_default_universe(date(2026, 1, 5))
        reliance = master.get("RELIANCE")

        self.assertEqual(reliance.segment, Segment.CASH)
        self.assertEqual(reliance.lot_size, 1)

    def test_select_future_skips_weekly_option_expiries(self):
        """Regression: NIFTY weekly options expire Tuesdays but futures expire
        monthly. select_future used the mixed nearest expiry (the weekly options
        date), found no futures contract there, and raised
        'No future contract found for NIFTY 2026-07-07' for every index."""
        def _inst(symbol: str, segment: Segment, itype: InstrumentType, expiry: date,
                  option_type: OptionType | None = None) -> Instrument:
            return Instrument(
                symbol=symbol, name=symbol, exchange=Exchange.NFO, segment=segment,
                asset_class=AssetClass.INDEX, instrument_type=itype, token=symbol,
                lot_size=75, expiry=expiry, option_type=option_type,
                strike=25000.0 if option_type else None, underlying="NIFTY",
            )

        master = InstrumentMaster({
            # Weekly option expiring tomorrow (Tuesday 2026-07-07)
            "NIFTY07JUL2625000CE": _inst(
                "NIFTY07JUL2625000CE", Segment.OPTIONS, InstrumentType.OPTION,
                date(2026, 7, 7), OptionType.CE),
            # Monthly future expiring end of month
            "NIFTY28JUL26FUT": _inst(
                "NIFTY28JUL26FUT", Segment.FUTURES, InstrumentType.FUTURE,
                date(2026, 7, 28)),
        })

        future = master.select_future("NIFTY", date(2026, 7, 6))
        self.assertEqual(future.expiry, date(2026, 7, 28))

        # Option selection still lands on the nearest weekly.
        option = master.select_option("NIFTY", date(2026, 7, 6), 25000.0, OptionType.CE)
        self.assertEqual(option.expiry, date(2026, 7, 7))

    def test_includes_stock_futures_and_options(self):
        master = build_default_universe(date(2026, 1, 5))
        future = master.select_future("RELIANCE", date(2026, 1, 5))
        option = master.select_option("TCS", date(2026, 1, 5), 3500, OptionType.PE)

        self.assertEqual(future.segment, Segment.FUTURES)
        self.assertEqual(future.underlying, "RELIANCE")
        self.assertEqual(option.segment, Segment.OPTIONS)
        self.assertEqual(option.option_type, OptionType.PE)


if __name__ == "__main__":
    unittest.main()
